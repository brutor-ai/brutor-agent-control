// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # LLM dispatch-site resilience
//!
//! One entry point — [`gate_dispatch`] — that every hot-path handler
//! calls before invoking the actual LLM upstream. Combines three
//! concerns that used to be separate / missing:
//!
//!   1. **Health-aware routing** — if `llm_model.health_status ==
//!      "unhealthy"` and the request has fallback models, skip the
//!      primary entirely instead of burning retries against an
//!      already-known-bad upstream.
//!   2. **Circuit breaker** — a per-model breaker trips after N
//!      consecutive failures and rejects fast for a cooldown window.
//!      Complements RouterState cooldowns (which live in Redis and
//!      track explicit error status codes) with an in-process,
//!      hot-path-cheap layer that also catches connection errors.
//!   3. **Concurrency limiter** — per-model semaphore caps simultaneous
//!      in-flight requests. Protects self-hosted vLLM / reasoning
//!      models from head-of-line blocking under bursty load where
//!      rpm/tpm rate limits aren't tight enough.
//!
//! All three concerns emit **system alerts** on notable transitions —
//! circuit open, health-failover, semaphore saturation. The alerts
//! are deduplicated by the existing `system_alerts` pipeline so a
//! broken provider produces one row, not one-per-request.

use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::models::db::{LlmModel, ResourceGroup};
use crate::services::SharedState;
use crate::services::circuit_breaker::CircuitOpenError;
use crate::services::system_alerts::{SystemAlertInput, spawn_raise};
use crate::services::usage::{LlmConcurrencyGuard, acquire_llm_concurrency};

/// Reasons the resilience layer refused to dispatch to a given model.
#[derive(Debug)]
pub enum GateRejection {
    /// The model's `health_status` is `unhealthy`. Caller should
    /// try the next fallback model.
    Unhealthy {
        model_id: String,
        last_error: Option<String>,
    },
    /// The model's circuit breaker is open. Caller should try the
    /// next fallback model (the upstream is being rejected fast to
    /// give it time to recover).
    CircuitOpen {
        model_id: String,
        retry_after: Duration,
    },
    /// Timed out waiting for a concurrency permit on this model.
    /// Distinct from CircuitOpen — the upstream is reachable, we
    /// just have too many in-flight calls already.
    ConcurrencySaturated {
        model_id: String,
        max_concurrent: i32,
        waited_ms: u64,
    },
    /// A resource-group / per-model concurrency cap from
    /// `llm_global_limits.concurrency.max_concurrent` (cluster-wide,
    /// Redis-backed) is saturated. Distinct from `ConcurrencySaturated`,
    /// which is the per-pod in-process semaphore on the model's
    /// `max_concurrent_requests` column. Maps to 429. Unlike the health/
    /// circuit rejections there is no point failing over to a sibling
    /// model at group scope (they share the counter), but the caller's
    /// existing fallback loop degrades to 429 either way.
    RgConcurrencySaturated {
        scope: &'static str,
        message: String,
    },
}

impl std::fmt::Display for GateRejection {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            GateRejection::Unhealthy { model_id, .. } => {
                write!(f, "model '{model_id}' is unhealthy — skipping to fallback")
            }
            GateRejection::CircuitOpen {
                model_id,
                retry_after,
            } => write!(
                f,
                "model '{model_id}' circuit is open — retry after {:.1}s",
                retry_after.as_secs_f64(),
            ),
            GateRejection::ConcurrencySaturated {
                model_id,
                max_concurrent,
                waited_ms,
            } => write!(
                f,
                "model '{model_id}' concurrency cap {max_concurrent} hit \
                 — waited {waited_ms}ms without a slot",
            ),
            GateRejection::RgConcurrencySaturated { message, .. } => write!(f, "{message}"),
        }
    }
}

impl std::error::Error for GateRejection {}

/// Convert a `GateRejection` into the canonical HTTP error
/// response. Used by handlers that don't have a fallback list (the
/// chat handler hands off to `forward_with_fallbacks` instead).
///
/// Status-code mapping:
///   * `Unhealthy` → 503 Service Unavailable. The model is gated
///     out by `mcp_health::record_llm_failure`. Retrying will
///     either trip the recovery probe path (one request every 10s
///     can attempt the upstream) or stay blocked until an
///     operator marks the model healthy.
///   * `CircuitOpen` → 503 + `Retry-After` header. The breaker
///     has tripped; retrying *now* hits the same fast-fail path,
///     but retrying after `retry_after` seconds may succeed.
///   * `ConcurrencySaturated` → 429 Too Many Requests. The
///     upstream is reachable; the tenant is just at the
///     `max_concurrent_requests` cap. Backoff is the right
///     client-side reaction.
pub fn gate_rejection_response(rej: &GateRejection) -> axum::response::Response {
    use axum::http::StatusCode;
    use axum::http::header::{HeaderName, HeaderValue, RETRY_AFTER};
    use axum::response::IntoResponse;

    match rej {
        GateRejection::Unhealthy {
            model_id,
            last_error,
        } => (
            StatusCode::SERVICE_UNAVAILABLE,
            axum::Json(serde_json::json!({
                "error": {
                    "type": "model_unhealthy",
                    "model_id": model_id,
                    "message": format!(
                        "Model '{model_id}' is currently marked unhealthy by \
                         the gateway. Most recent upstream error: {}",
                        last_error.as_deref().unwrap_or("(none)"),
                    ),
                }
            })),
        )
            .into_response(),
        GateRejection::CircuitOpen {
            model_id,
            retry_after,
        } => {
            let secs = retry_after.as_secs().max(1);
            let mut resp = (
                StatusCode::SERVICE_UNAVAILABLE,
                axum::Json(serde_json::json!({
                    "error": {
                        "type": "circuit_open",
                        "model_id": model_id,
                        "retry_after_seconds": secs,
                        "message": format!(
                            "Model '{model_id}' circuit breaker is open — \
                             retry after {secs}s",
                        ),
                    }
                })),
            )
                .into_response();
            if let Ok(v) = HeaderValue::from_str(&secs.to_string()) {
                resp.headers_mut().insert(RETRY_AFTER, v);
            }
            resp
        }
        GateRejection::ConcurrencySaturated {
            model_id,
            max_concurrent,
            waited_ms,
        } => {
            let mut resp = (
                StatusCode::TOO_MANY_REQUESTS,
                axum::Json(serde_json::json!({
                    "error": {
                        "type": "concurrency_saturated",
                        "model_id": model_id,
                        "max_concurrent": max_concurrent,
                        "waited_ms": waited_ms,
                        "message": format!(
                            "Model '{model_id}' is at its concurrency cap \
                             ({max_concurrent}); waited {waited_ms}ms with \
                             no slot available.",
                        ),
                    }
                })),
            )
                .into_response();
            // Retry-After is a hint; even 1 second is enough to let
            // an in-flight request finish and free a permit.
            if let Ok(v) = HeaderValue::from_str("1") {
                resp.headers_mut()
                    .insert(HeaderName::from_static("retry-after"), v);
            }
            resp
        }
        GateRejection::RgConcurrencySaturated { scope, message } => {
            let mut resp = (
                StatusCode::TOO_MANY_REQUESTS,
                axum::Json(serde_json::json!({
                    "error": {
                        "type": "concurrency_saturated",
                        "scope": scope,
                        "message": message,
                    }
                })),
            )
                .into_response();
            if let Ok(v) = HeaderValue::from_str("1") {
                resp.headers_mut()
                    .insert(HeaderName::from_static("retry-after"), v);
            }
            resp
        }
    }
}

/// RAII guard returned by [`gate_dispatch`]. Holds a semaphore permit
/// (if a concurrency cap is configured) for the lifetime of the
/// dispatch. Call [`DispatchGuard::record_success`] /
/// [`DispatchGuard::record_failure`] / [`DispatchGuard::abandon`] /
/// [`DispatchGuard::cancelled`] exactly once before drop.
///
/// The Drop impl distinguishes three outcomes for an
/// unfinalized guard:
///
/// * **Cancelled** — task-local cancellation flag is true. Logged at
///   INFO; this is operational signal, not a bug.
/// * **Leaked** — flag is false (or unavailable) AND no finalize
///   method ran. **Indicates a developer bug** — a route handler
///   has a code path that doesn't consume the guard. Logged at WARN
///   with `bug_suspected=true` so the on-call grep finds it.
/// * **Abandoned / Cancelled (explicit)** — explicit finalize via
///   [`Self::abandon`] or [`Self::cancelled`]. Drop is silent.
pub struct DispatchGuard {
    state: SharedState,
    model_id: String,
    tenant_id: String,
    _permit: Option<OwnedSemaphorePermit>,
    /// Cluster-wide RG/per-model concurrency slots (`llm_global_limits.
    /// concurrency.max_concurrent`). Its own `Drop` releases the Redis
    /// slots; holding it here ties that release to the dispatch lifetime
    /// (streaming included), exactly like `_permit`.
    _rg_concurrency: Option<LlmConcurrencyGuard>,
    outcome_recorded: bool,
    finalize_state: GuardFinalizeState,
}

/// How the guard was finalized. Pinned at construction to
/// [`GuardFinalizeState::Pending`]; each finalize method moves it
/// to the appropriate terminal state. Drop reads it together with
/// the cancellation marker to classify the outcome.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GuardFinalizeState {
    /// No finalize method ran. Drop will classify based on the
    /// cancellation marker.
    Pending,
    /// `record_success` ran.
    RecordedSuccess,
    /// `record_failure` ran.
    RecordedFailure,
    /// `abandon` ran (input-validation early return — not an
    /// upstream failure).
    Abandoned,
    /// `cancelled` ran (explicit acknowledgement).
    Cancelled,
}

impl DispatchGuard {
    /// Record a successful upstream call. Resets the breaker's failure
    /// counter and transitions HalfOpen → Closed if applicable.
    pub fn record_success(mut self) {
        self.state
            .llm_circuit_breakers
            .record_success(&self.model_id);
        self.outcome_recorded = true;
        self.finalize_state = GuardFinalizeState::RecordedSuccess;
    }

    /// Consume the guard without feeding the breaker.
    ///
    /// Use when an early return *after* gate acquisition is caused by
    /// caller-side input validation (file format whitelist, quota
    /// caps reached on a downstream pre-check, content-policy etc.) —
    /// none of which represent an upstream failure. Counting them
    /// would falsely degrade the breaker's view of the model's
    /// health and could trip a cooldown on a perfectly working
    /// upstream.
    ///
    /// Releases the semaphore permit (it would on Drop anyway, but
    /// being explicit keeps the intent obvious at the call site) and
    /// suppresses the "outcome not recorded" warning in Drop.
    pub fn abandon(mut self) {
        self.outcome_recorded = true;
        self.finalize_state = GuardFinalizeState::Abandoned;
    }

    /// Acknowledge that the request was cancelled (client disconnect,
    /// upstream timeout race) before either record_success or
    /// record_failure could run. Distinct from [`Self::abandon`] —
    /// abandon means "we deliberately chose not to feed the
    /// breaker", cancelled means "we got cut off mid-flight".
    ///
    /// Most call sites don't need to call this manually — the
    /// task-local cancellation marker detects cancellation
    /// from the request future's Drop and the guard's own Drop
    /// reads it. Provided for code paths that want to be explicit.
    pub fn cancelled(mut self) {
        self.outcome_recorded = true;
        self.finalize_state = GuardFinalizeState::Cancelled;
    }

    /// Record a failed upstream call (any non-success, any transport
    /// error). Increments the consecutive-failure counter and may
    /// trip the breaker to Open — in which case a
    /// `downstream_provider` system alert is raised so the on-call
    /// engineer learns about the outage deduped with all the other
    /// requests hitting the same provider.
    pub fn record_failure(mut self, detail: impl Into<String>) {
        let model_id = self.model_id.clone();
        let just_opened = self.state.llm_circuit_breakers.record_failure(&model_id);
        self.outcome_recorded = true;
        self.finalize_state = GuardFinalizeState::RecordedFailure;

        if just_opened {
            let detail_str: String = detail.into();
            tracing::warn!(
                model_id = %model_id,
                %detail_str,
                "LLM circuit breaker opened — blocking requests for cooldown window"
            );
            // Dedup-friendly alert: tenant-scoped, error_code
            // encodes the model so repeat trips on different models
            // stay as separate rows.
            spawn_raise(
                self.state.db.clone(),
                SystemAlertInput::downstream_provider(
                    Some(self.tenant_id.clone()),
                    "llm",
                    Some("llm_model".to_string()),
                    Some(model_id.clone()),
                    Some(format!("llm_circuit_opened:{model_id}")),
                    format!(
                        "LLM circuit breaker opened for model '{model_id}' — \
                         {detail_str}. Requests will short-circuit until the \
                         cooldown window elapses and a probe succeeds."
                    ),
                ),
            );
        }
    }
}

static LEAKED_GUARDS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// How many dispatch guards this process dropped without finalizing them
/// outside a cancelled request (`dispatch_guard_drop_total{reason="leaked"}`):
/// each is a code path that returned without closing its guard. A test and
/// diagnostic hook.
pub fn leaked_guards() -> u64 {
    LEAKED_GUARDS.load(std::sync::atomic::Ordering::Relaxed)
}

impl Drop for DispatchGuard {
    fn drop(&mut self) {
        // Classify the drop into one of:
        //   "recorded"   — finalize ran (success / failure / abandoned / cancelled). Silent.
        //   "cancelled"  — finalize did NOT run AND the request future's
        //                  cancellation marker is set. Operational signal,
        //                  not a bug. INFO log + counter.
        //   "leaked"     — finalize did NOT run AND the cancellation marker
        //                  is unset (or unavailable). **Developer bug** —
        //                  WARN log with bug_suspected=true so the on-call
        //                  grep finds it.
        let reason = if self.outcome_recorded {
            // Map the finalize state to a stable label for the
            // counter — `recorded_*` stays narrow so a Loki query
            // can distinguish success, failure, abandon, cancel.
            match self.finalize_state {
                GuardFinalizeState::RecordedSuccess => "recorded_success",
                GuardFinalizeState::RecordedFailure => "recorded_failure",
                GuardFinalizeState::Abandoned => "abandoned",
                GuardFinalizeState::Cancelled => "cancelled_explicit",
                // Defensive: outcome_recorded=true with state=Pending
                // would be a logic bug. Tag it so it's visible.
                GuardFinalizeState::Pending => "recorded_unknown",
            }
        } else {
            // Pending finalize. Distinguish cancellation vs. leak via
            // the task-local marker.
            match crate::middleware::cancellation_marker::was_cancelled() {
                Some(true) => {
                    tracing::info!(
                        target: "dispatch_guard",
                        model_id = %self.model_id,
                        tenant_id = %self.tenant_id,
                        "DispatchGuard dropped on request cancellation \
                         — permit auto-released, breaker not updated"
                    );
                    "cancelled"
                }
                Some(false) | None => {
                    tracing::warn!(
                        target: "dispatch_guard",
                        model_id = %self.model_id,
                        tenant_id = %self.tenant_id,
                        bug_suspected = true,
                        "DispatchGuard dropped without finalize and no \
                         cancellation marker — caller has a code path \
                         that doesn't consume the guard"
                    );
                    LEAKED_GUARDS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    "leaked"
                }
            }
        };
        // Counter — flat label set, low cardinality. Operators
        // alert on `dispatch_guard_drop_total{reason="leaked"} > 0`
        // sustained.
        metrics::counter!(
            "dispatch_guard_drop_total",
            "reason" => reason.to_string(),
        )
        .increment(1);
        // OwnedSemaphorePermit auto-releases when `_permit` drops below.
    }
}

/// Maximum time to wait for a concurrency permit before giving up and
/// returning `ConcurrencySaturated`. Kept short so clients don't
/// queue up on a blocked model — the caller should fail over or
/// return 503 immediately.
const PERMIT_WAIT_TIMEOUT: Duration = Duration::from_millis(500);

/// Minimum interval between recovery probes for a model that is
/// `health_status='unhealthy'` in the DB. When this window has
/// elapsed since the last probe we allow exactly one request through
/// (CAS-guarded via `llm_health_probes`). That request's outcome is
/// recorded by `mcp_health::record_llm_success/failure`, so a
/// recovered upstream flips the DB flag back to `healthy` on the
/// next response.
///
/// Without this path, a model that ever hit `consecutive_health_failures
/// >= 4` would be stuck `unhealthy` forever — the gate would block
/// every request, so `record_llm_success` could never fire. That's
/// > the stuck-unhealthy bug observed in production.
const HEALTH_PROBE_INTERVAL: Duration = Duration::from_secs(10);

/// Clear every piece of in-memory resilience state for a model. Used
/// by the admin "Test connection → Healthy" flow so an operator can
/// unstick a model without waiting for the natural probe cadence.
///
/// Touches:
///   - circuit breaker (resets counters, forces Closed)
///   - concurrency semaphore (no-op; permits auto-release on drop)
///   - probe-throttle map entry
///   - router_state cooldown
///   - DB health_status → 'healthy' (caller should already have
///     updated it via `update_health_status`, but we ensure it here
///     too because the gate reads this column directly)
///
/// This is fire-and-forget best-effort: the circuit-breaker and
/// probe-map writes are in-process and always succeed; the DB write
/// is spawned so the admin request doesn't block on DB latency.
/// Clear ONLY the in-process resilience state for a model: circuit
/// breaker, probe throttle, and router cooldown. No DB write.
///
/// Use when the caller has already updated `llm_model.health_status`
/// itself (e.g. the admin reset-resilience endpoint, which collapses
/// the in-memory clear and the DB flush into a single transaction
/// to keep its connection-pool footprint small).
pub fn reset_model_resilience_in_memory(state: &SharedState, model_id: &str) {
    state.llm_circuit_breakers.record_success(model_id);
    state.llm_health_probes.remove(model_id);
    state.router_state.record_success(model_id);
}

/// Full reset: in-process state plus a fire-and-forget DB UPDATE to
/// flip `llm_model.health_status` back to `healthy`. Use from code
/// paths that don't already touch the DB row (e.g. the cooldown
/// success path that observes a model recovering on its own).
pub fn reset_model_resilience(state: &SharedState, tenant_id: &str, model_id: &str) {
    reset_model_resilience_in_memory(state, model_id);

    // DB flag — fire-and-forget so the caller doesn't pay the DB
    // round-trip on the hot path.
    let db = state.db.clone();
    let tid = tenant_id.to_string();
    let mid = model_id.to_string();
    tokio::spawn(async move {
        let sql = "UPDATE llm_model SET health_status = 'healthy', \
                   consecutive_health_failures = 0, \
                   health_check_error = NULL, \
                   last_health_success = NOW(), \
                   last_health_check = NOW() \
                   WHERE id = $1 AND tenant_id = $2";
        if let Err(e) = sqlx::query(sql).bind(&mid).bind(&tid).execute(&db).await {
            tracing::error!(
                model_id = %mid,
                error = %e,
                "Failed to reset llm_model health_status during admin reset"
            );
        }
    });

    // Routine bookkeeping — fires on every successful probe and
    // on startup for every model. Not an on-call signal, so per
    // the proxy log taxonomy this is a `debug` event.
    tracing::debug!(
        model_id = %model_id,
        tenant_id = %tenant_id,
        "Resilience state reset — breaker, probe throttle, cooldown, and DB flag cleared"
    );
}

/// Atomic check-and-set on the probe-throttle map. Returns `true` if
/// the caller just claimed the probe slot for `model_id` (either no
/// previous probe, or the last probe is older than `interval`); any
/// concurrent caller in the same interval gets `false`.
///
/// Split out from [`gate_dispatch`] so it's unit-testable without
/// standing up a SharedState with a live DB pool.
fn try_claim_probe_slot(
    probes: &dashmap::DashMap<String, Instant>,
    model_id: &str,
    interval: Duration,
    now: Instant,
) -> bool {
    let mut granted = false;
    probes
        .entry(model_id.to_string())
        .and_modify(|last| {
            if now.duration_since(*last) >= interval {
                *last = now;
                granted = true;
            }
        })
        .or_insert_with(|| {
            granted = true;
            now
        });
    granted
}

/// Gate an LLM dispatch through all three resilience layers. Returns
/// an RAII [`DispatchGuard`] the caller must finalise with
/// `record_success` or `record_failure` after the upstream call.
///
/// Fast-failing with `GateRejection` is the expected hot path for a
/// degraded upstream — the caller should log-and-continue to the
/// next fallback rather than 500 immediately.
pub async fn gate_dispatch(
    state: &SharedState,
    model: &LlmModel,
    tenant_id: &str,
    group: Option<&ResourceGroup>,
) -> Result<DispatchGuard, GateRejection> {
    // 1. Health-aware routing with bounded-probe recovery.
    //
    //    `healthy` and NULL both proceed unconditionally (NULL = "not
    //    yet probed"). `degraded` proceeds too — retries are still a
    //    reasonable bet at that confidence level.
    //
    //    `unhealthy` normally short-circuits, BUT we let exactly one
    //    request through every `HEALTH_PROBE_INTERVAL` so a recovered
    //    upstream has a path back to `healthy`. The probe's outcome
    //    is recorded by the caller through `mcp_health::record_llm_*`,
    //    which writes `health_status='healthy'` on success — closing
    //    the loop without a separate background worker.
    //
    //    The throttle is CAS-guarded via the in-process
    //    `llm_health_probes` map: the first request whose stored
    //    timestamp is older than the interval wins the probe slot;
    //    concurrent requests in the same window fail over as usual.
    if model.health_status.as_deref() == Some("unhealthy") {
        let allow_probe = try_claim_probe_slot(
            &state.llm_health_probes,
            &model.id,
            HEALTH_PROBE_INTERVAL,
            Instant::now(),
        );

        if !allow_probe {
            tracing::debug!(
                model_id = %model.id,
                health_status = ?model.health_status,
                "Gating dispatch: model is unhealthy — caller should fail over"
            );
            return Err(GateRejection::Unhealthy {
                model_id: model.id.clone(),
                last_error: model.health_check_error.clone(),
            });
        }

        tracing::debug!(
            model_id = %model.id,
            "Allowing recovery probe — model is unhealthy but probe window elapsed"
        );
        // Fall through to breaker + concurrency checks. If this
        // request succeeds, `record_llm_success` clears the DB flag;
        // if it fails, `record_llm_failure` keeps us in the unhealthy
        // state and the next probe won't be allowed until another
        // `HEALTH_PROBE_INTERVAL` passes.
    }

    // 2. Circuit breaker. Per-model trip state, separate from the
    //    MCP-side registry so thresholds can be tuned independently.
    //    If the model carries explicit `allowed_fails` /
    //    `cooldown_time` (admin override) we configure the breaker
    //    with those values on the fly.
    if let (Some(fails), Some(cooldown_s)) = (model.allowed_fails, model.cooldown_time) {
        state.llm_circuit_breakers.configure(
            &model.id,
            fails.max(1) as u32,
            Duration::from_secs(cooldown_s.max(1) as u64),
        );
    }

    if let Err(CircuitOpenError { remaining }) = state.llm_circuit_breakers.check_allowed(&model.id)
    {
        return Err(GateRejection::CircuitOpen {
            model_id: model.id.clone(),
            retry_after: remaining,
        });
    }

    // 3. Concurrency limiter. Get-or-create the semaphore on first
    //    use; capacity locks on creation. If the admin later raises
    //    `max_concurrent_requests`, the semaphore keeps its old
    //    capacity until the next process restart — documented on the
    //    admin field. `None` or non-positive values mean "unbounded"
    //    and we skip semaphore acquisition entirely to avoid the
    //    DashMap write on every request.
    let permit = match model.max_concurrent_requests {
        Some(cap) if cap > 0 => {
            let sem = state
                .llm_concurrency
                .entry(model.id.clone())
                .or_insert_with(|| Arc::new(Semaphore::new(cap as usize)))
                .value()
                .clone();
            let wait_start = Instant::now();
            match tokio::time::timeout(PERMIT_WAIT_TIMEOUT, sem.clone().acquire_owned()).await {
                Ok(Ok(permit)) => Some(permit),
                Ok(Err(_closed)) => {
                    // Semaphore closed — shouldn't happen in prod,
                    // but treat as saturation.
                    return Err(GateRejection::ConcurrencySaturated {
                        model_id: model.id.clone(),
                        max_concurrent: cap,
                        waited_ms: wait_start.elapsed().as_millis() as u64,
                    });
                }
                Err(_) => {
                    let waited = wait_start.elapsed();
                    tracing::warn!(
                        model_id = %model.id,
                        max_concurrent = cap,
                        waited_ms = waited.as_millis() as u64,
                        "LLM concurrency cap saturated — refusing dispatch"
                    );
                    // Dedup alert per model via downstream_provider —
                    // capacity-side "upstream can't keep up" is the
                    // same operator concern as "upstream is erroring"
                    // and we want one pane of glass for both.
                    spawn_raise(
                        state.db.clone(),
                        SystemAlertInput::downstream_provider(
                            Some(tenant_id.to_string()),
                            &model.provider,
                            Some(model.id.clone()),
                            Some(model.model_name.clone()),
                            Some(format!("llm_concurrency_saturated:{}", model.id)),
                            format!(
                                "LLM model '{}' hit its concurrency cap ({cap}) — \
                                 callers are queueing and timing out after \
                                 {}ms. Raise max_concurrent_requests or add \
                                 more model instances behind a routing group.",
                                model.id,
                                PERMIT_WAIT_TIMEOUT.as_millis(),
                            ),
                        ),
                    );
                    return Err(GateRejection::ConcurrencySaturated {
                        model_id: model.id.clone(),
                        max_concurrent: cap,
                        waited_ms: waited.as_millis() as u64,
                    });
                }
            }
        }
        _ => None,
    };

    // 4. Cluster-wide RG / per-model concurrency (`llm_global_limits.
    //    concurrency.max_concurrent`, group + model scope, restrictive/
    //    stacking like every other limit). Acquired AFTER the in-process
    //    permit so a rejection here drops `permit` (local) on the early
    //    return — no leak. `None` group → skipped (nothing to resolve
    //    limits against).
    let rg_concurrency = match group {
        Some(g) => match acquire_llm_concurrency(
            &state.usage_state,
            &state.db,
            g,
            &model.id,
            // Each dispatch attempt (primary, then each fallback model) holds
            // its own slots.
            model.routed_via_group_id.as_deref(),
        )
        .await
        {
            Ok(guard) => Some(guard),
            Err(block) => {
                return Err(GateRejection::RgConcurrencySaturated {
                    scope: block.scope,
                    message: block.message,
                });
            }
        },
        None => None,
    };

    Ok(DispatchGuard {
        state: state.clone(),
        model_id: model.id.clone(),
        tenant_id: tenant_id.to_string(),
        _permit: permit,
        _rg_concurrency: rg_concurrency,
        outcome_recorded: false,
        finalize_state: GuardFinalizeState::Pending,
    })
}

pub fn raise_failover_alert(
    state: &SharedState,
    tenant_id: &str,
    primary_id: &str,
    primary_error: &GateRejection,
    fallback_id: &str,
) {
    spawn_raise(
        state.db.clone(),
        SystemAlertInput::resource_health_change(
            Some(tenant_id.to_string()),
            "llm_model",
            primary_id,
            Some(primary_id.to_string()),
            "unhealthy",
            format!(
                "Primary LLM '{primary_id}' rejected at dispatch ({primary_error}); \
                 failing over to '{fallback_id}'. Alert will auto-resolve when \
                 the primary's circuit closes / health flips back to healthy.",
            ),
        ),
    );
}

/// Convenience: raise a health-failover alert when a caller falls
/// over from the primary model to a fallback because the primary was
/// rejected by the gate. Separated from `gate_dispatch` so callers
/// only emit the alert on the path that *actually* used a fallback
/// (not every rejection — e.g., a 429 that retries on the primary
/// successfully shouldn't alarm anyone).
#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::circuit_breaker::CircuitBreakerRegistry;
    use dashmap::DashMap;

    // Small constructors for the pieces `gate_dispatch` needs — full
    // SharedState would require a live DB pool and HTTP client, which
    // these unit tests deliberately avoid. Instead we assert against
    // the component behaviour (breaker trip state, semaphore
    // acquisition) directly.

    #[test]
    fn circuit_open_on_threshold_reached() {
        let registry = CircuitBreakerRegistry::new();
        registry.configure("model-x", 3, Duration::from_secs(60));
        // Three failures — last one trips.
        assert!(!registry.record_failure("model-x"));
        assert!(!registry.record_failure("model-x"));
        let just_opened = registry.record_failure("model-x");
        assert!(just_opened, "third failure should open the breaker");
        assert!(registry.check_allowed("model-x").is_err());
    }

    #[test]
    fn semaphore_bounds_concurrent_acquisitions() {
        // The concurrency limiter is a standard Semaphore — capping
        // concurrent permits is what matters; verify directly.
        let semaphores: DashMap<String, Arc<Semaphore>> = DashMap::new();
        let sem = semaphores
            .entry("model-y".to_string())
            .or_insert_with(|| Arc::new(Semaphore::new(2)))
            .value()
            .clone();

        let p1 = sem.clone().try_acquire_owned();
        let p2 = sem.clone().try_acquire_owned();
        let p3 = sem.clone().try_acquire_owned();
        assert!(p1.is_ok());
        assert!(p2.is_ok());
        assert!(p3.is_err(), "third acquire should fail (cap=2)");

        drop(p1);
        // Now a new acquire should succeed again.
        assert!(sem.clone().try_acquire_owned().is_ok());
    }

    #[tokio::test]
    async fn semaphore_wait_honours_timeout() {
        // Simulate the saturation path: hold all permits, then ensure
        // a waiter times out within the configured wait-limit.
        let sem = Arc::new(Semaphore::new(1));
        let _held = sem.clone().acquire_owned().await.unwrap();

        let result =
            tokio::time::timeout(Duration::from_millis(50), sem.clone().acquire_owned()).await;
        assert!(
            result.is_err(),
            "waiter should time out when permits are held"
        );
    }

    #[test]
    fn gate_rejection_display_is_informative() {
        // Display strings end up in audit logs / 503 bodies — make
        // sure they include the model id and the reason so on-call
        // can act without grepping the code.
        let unhealthy = GateRejection::Unhealthy {
            model_id: "m-1".into(),
            last_error: Some("timeout".into()),
        };
        let msg = unhealthy.to_string();
        assert!(msg.contains("m-1"));
        assert!(msg.contains("unhealthy"));

        let open = GateRejection::CircuitOpen {
            model_id: "m-2".into(),
            retry_after: Duration::from_secs(15),
        };
        let msg = open.to_string();
        assert!(msg.contains("m-2"));
        assert!(msg.contains("15"));

        let saturated = GateRejection::ConcurrencySaturated {
            model_id: "m-3".into(),
            max_concurrent: 4,
            waited_ms: 500,
        };
        let msg = saturated.to_string();
        assert!(msg.contains("m-3"));
        assert!(msg.contains("4"));
        assert!(msg.contains("500"));

        // RG concurrency saturation carries the block's own message verbatim.
        let rg = GateRejection::RgConcurrencySaturated {
            scope: "group",
            message: "LLM concurrency cap reached at group scope (10 simultaneous)".into(),
        };
        let msg = rg.to_string();
        assert!(msg.contains("group scope"));
        assert!(msg.contains("10"));
    }

    #[test]
    fn rg_concurrency_saturation_maps_to_429_with_retry_after() {
        // The cluster-wide RG/per-model concurrency cap
        // (`llm_global_limits.concurrency`) is a "reachable, just full"
        // condition — same 429 + Retry-After posture as the per-pod
        // semaphore saturation, distinct from the 503 health/circuit paths.
        let resp = gate_rejection_response(&GateRejection::RgConcurrencySaturated {
            scope: "model",
            message: "LLM concurrency cap reached at model scope (2 simultaneous)".into(),
        });
        assert_eq!(resp.status(), axum::http::StatusCode::TOO_MANY_REQUESTS);
        assert!(resp.headers().contains_key(axum::http::header::RETRY_AFTER));
    }

    // ==================================================================
    // Probe-throttle (the stuck-unhealthy recovery path)
    // ==================================================================

    #[test]
    fn probe_slot_first_claim_is_granted() {
        let probes: DashMap<String, Instant> = DashMap::new();
        let now = Instant::now();
        assert!(try_claim_probe_slot(
            &probes,
            "m-new",
            Duration::from_secs(60),
            now
        ));
        assert_eq!(probes.len(), 1);
    }

    #[test]
    fn probe_slot_second_claim_within_window_is_rejected() {
        let probes: DashMap<String, Instant> = DashMap::new();
        let now = Instant::now();

        // First claim wins the slot for this interval.
        assert!(try_claim_probe_slot(
            &probes,
            "m-a",
            Duration::from_secs(60),
            now
        ));
        // Second claim 1ms later must be denied — the point of the
        // throttle is to send exactly one probe per window.
        assert!(!try_claim_probe_slot(
            &probes,
            "m-a",
            Duration::from_secs(60),
            now + Duration::from_millis(1),
        ));
    }

    #[test]
    fn probe_slot_granted_again_after_interval_elapses() {
        // Models that stay unhealthy must get a fresh probe every
        // interval — otherwise a recovered provider never gets a
        // chance to flip its own health flag back to healthy.
        let probes: DashMap<String, Instant> = DashMap::new();
        let now = Instant::now();

        assert!(try_claim_probe_slot(
            &probes,
            "m-b",
            Duration::from_secs(60),
            now
        ));
        assert!(!try_claim_probe_slot(
            &probes,
            "m-b",
            Duration::from_secs(60),
            now + Duration::from_secs(30),
        ));
        assert!(try_claim_probe_slot(
            &probes,
            "m-b",
            Duration::from_secs(60),
            now + Duration::from_secs(61),
        ));
    }

    #[test]
    fn probe_slot_per_model_isolation() {
        // A rejection for model-a must not consume model-b's slot.
        let probes: DashMap<String, Instant> = DashMap::new();
        let now = Instant::now();
        assert!(try_claim_probe_slot(
            &probes,
            "m-a",
            Duration::from_secs(60),
            now
        ));
        // m-b is untouched → its first claim still wins.
        assert!(try_claim_probe_slot(
            &probes,
            "m-b",
            Duration::from_secs(60),
            now
        ));
    }
}

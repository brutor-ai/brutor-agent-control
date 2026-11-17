// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Retry with Exponential Backoff
//!
//! Retry logic for MCP upstream requests with circuit breaker integration.
//! Supports exponential backoff, `Retry-After` header parsing, and
//! automatic classification of retryable vs. non-retryable errors.

use std::sync::atomic::{AtomicI64, Ordering};
use std::time::Duration;

use dashmap::DashMap;
use rand::Rng;
use reqwest::header::HeaderMap;

use crate::services::circuit_breaker::CircuitBreakerRegistry;

/// Configuration for retry behavior.
pub struct RetryConfig {
    /// Maximum number of retry attempts (not counting the initial request).
    pub max_retries: u32,
    /// Base delay before the first retry.
    pub base_delay: Duration,
    /// Multiplicative factor applied to the delay on each subsequent retry.
    pub backoff_factor: u32,
    /// Whether the request is safe to auto-retry after an **ambiguous**
    /// failure — one where the upstream may have already processed the
    /// request (timeout, request error, or a 502/503/504 that an
    /// intermediary could have returned after forwarding).
    ///
    /// `true` (default) for idempotent requests (GET, MCP `tools/list`,
    /// reads, LLM completions): retry on the full transient set.
    ///
    /// `false` for **non-idempotent** requests — notably an MCP `tools/call`,
    /// whose tool may have side effects (send an email, create a ticket).
    /// Those retry ONLY when the upstream provably did not process the
    /// request: a connection error (never reached the server) or HTTP 429
    /// (explicitly rejected before processing). A timeout/502/504 surfaces
    /// to the client instead of silently re-executing the side effect.
    pub idempotent: bool,
    /// The call's deadline, when it has one: no retry waits past it (the
    /// last error is returned instead of sleeping beyond it).
    pub deadline: Option<tokio::time::Instant>,
}

impl RetryConfig {
    /// Whether waiting `backoff` before the next attempt would pass the
    /// call's deadline.
    fn waits_past_deadline(&self, backoff: Duration) -> bool {
        self.deadline
            .is_some_and(|d| tokio::time::Instant::now() + backoff >= d)
    }
}

impl Default for RetryConfig {
    fn default() -> Self {
        Self {
            max_retries: 2,
            base_delay: Duration::from_millis(100),
            backoff_factor: 4,
            idempotent: true,
            deadline: None,
        }
    }
}

// ---------------------------------------------------------------------------
// Retry budget
//
// Envoy-style global cap on retries as a fraction of in-flight calls, per
// circuit_key (model/server). Without this, a partial upstream outage that
// returns 503 on 40% of calls triples effective load on the provider — every
// failed request retries up to `max_retries` times, piling on when the
// provider is already struggling.
//
// Policy: retries_allowed = max(min_retries_in_flight, active_requests *
// ratio). A call may attempt a retry only if `active_retries < allowed`.
// Otherwise the retry is refused and the original error bubbles up.
//
// `active_requests` is fed by the caller (llm_router already tracks this in
// RouterState). `active_retries` is tracked here — incremented on retry
// start, decremented after the attempt completes regardless of outcome.
// ---------------------------------------------------------------------------

/// Runtime state for the retry budget. One instance per process; lives on
/// `AppState` alongside the circuit breaker registry.
pub struct RetryBudget {
    active_retries: DashMap<String, AtomicI64>,
    /// Minimum retries allowed even when in-flight is 0 (cold start).
    min_concurrent_retries: i64,
    /// Max retries as a fraction of in-flight requests (0.2 = 20%).
    retry_ratio: f64,
}

impl RetryBudget {
    /// Default budget: at least 3 concurrent retries, or 20% of in-flight.
    pub fn new() -> Self {
        Self {
            active_retries: DashMap::new(),
            min_concurrent_retries: 3,
            retry_ratio: 0.2,
        }
    }

    pub fn with_ratio(mut self, ratio: f64) -> Self {
        self.retry_ratio = ratio;
        self
    }

    pub fn with_min_concurrent(mut self, min: i64) -> Self {
        self.min_concurrent_retries = min;
        self
    }

    /// Attempt to reserve a retry slot for `key`. Returns `true` if
    /// allowed; the caller MUST then call `release()` on completion.
    /// Returns `false` if budget is exhausted — caller should fail fast.
    pub fn try_acquire(&self, key: &str, active_requests: i64) -> bool {
        let allowed = self
            .min_concurrent_retries
            .max((active_requests as f64 * self.retry_ratio).ceil() as i64);
        let counter = self
            .active_retries
            .entry(key.to_string())
            .or_insert_with(|| AtomicI64::new(0));
        // Optimistic increment; roll back if we went over budget.
        let prev = counter.value().fetch_add(1, Ordering::Relaxed);
        if prev + 1 > allowed {
            counter.value().fetch_sub(1, Ordering::Relaxed);
            false
        } else {
            true
        }
    }

    pub fn release(&self, key: &str) {
        if let Some(entry) = self.active_retries.get(key) {
            entry.value().fetch_sub(1, Ordering::Relaxed);
        }
    }

    pub fn active(&self, key: &str) -> i64 {
        self.active_retries
            .get(key)
            .map(|e| e.value().load(Ordering::Relaxed))
            .unwrap_or(0)
    }
}

impl Default for RetryBudget {
    fn default() -> Self {
        Self::new()
    }
}

/// Errors that can occur during retry execution.
#[derive(Debug)]
pub enum RetryError {
    /// The circuit breaker is open for this upstream — request was not attempted.
    CircuitOpen(String),
    /// Retry budget exhausted — the first attempt failed but the cap on
    /// concurrent retries for this key prevented further attempts. Prevents
    /// thundering-herd amplification during partial outages.
    BudgetExhausted {
        last_error: String,
        active_retries: i64,
    },
    /// All retry attempts were exhausted without a successful response.
    AllRetriesExhausted {
        /// Description of the last error encountered.
        last_error: String,
        /// Total number of attempts made (initial + retries).
        attempts: u32,
    },
    /// The upstream returned a non-retryable error status (e.g. 400, 401, 403, 404).
    NonRetryable(reqwest::Response),
}

/// Check if an HTTP status code is retryable.
///
/// Retryable statuses: 429 (rate limited), 502 (bad gateway),
/// 503 (service unavailable), 504 (gateway timeout).
pub fn is_retryable_status(status: u16) -> bool {
    matches!(status, 429 | 502 | 503 | 504)
}

/// Check if a reqwest error is retryable (connection, timeout, or request errors).
pub fn is_retryable_error(err: &reqwest::Error) -> bool {
    err.is_connect() || err.is_timeout() || err.is_request()
}

/// Idempotency-aware status retryability.
///
/// Idempotent requests retry on the full transient set (429/502/503/504).
/// Non-idempotent requests retry ONLY on 429: a 429 means the upstream
/// rejected the request before processing it, so re-sending cannot duplicate
/// a side effect. A 502/503/504 is ambiguous (an intermediary may have
/// forwarded the request and the backend processed it), so it is treated as
/// non-retryable for a non-idempotent call and surfaced to the client.
pub fn status_retryable_for(status: u16, idempotent: bool) -> bool {
    if idempotent {
        is_retryable_status(status)
    } else {
        status == 429
    }
}

/// Idempotency-aware transport-error retryability.
///
/// Idempotent requests retry on connect, timeout, and request errors.
/// Non-idempotent requests retry ONLY on a connection error — the one case
/// that guarantees the request never reached the upstream, so the tool could
/// not have run. A timeout is explicitly NOT retried for non-idempotent
/// calls: the request may have been received and the side effect already
/// executed (the bug this guards against — a slow `tools/call` running 3×).
pub fn error_retryable_for(err: &reqwest::Error, idempotent: bool) -> bool {
    if idempotent {
        is_retryable_error(err)
    } else {
        err.is_connect()
    }
}

/// Parse the `Retry-After` header value.
///
/// Supports two formats per RFC 7231:
/// - Integer seconds (e.g. `Retry-After: 120`)
/// - HTTP-date (e.g. `Retry-After: Thu, 01 Jan 2026 00:00:00 GMT`)
///
/// Returns `None` if the header is missing or cannot be parsed.
pub fn extract_retry_after(headers: &HeaderMap) -> Option<Duration> {
    let value = headers.get("retry-after")?.to_str().ok()?;

    // Parse as integer seconds (the most common Retry-After format).
    // HTTP-date format is rare for API servers; skip to avoid extra dependency.
    if let Ok(secs) = value.parse::<u64>() {
        return Some(Duration::from_secs(secs));
    }

    None
}

/// Execute an async request with retry and circuit breaker integration.
///
/// The closure `make_request` must rebuild the request on each invocation
/// because `RequestBuilder` is consumed on `.send()`.
///
/// # Flow
/// 1. Check the circuit breaker — if open, return `CircuitOpen` immediately.
/// 2. Attempt up to `max_retries + 1` total requests.
/// 3. On success (non-retryable status): record success with circuit breaker, return response.
/// 4. On retryable status: record failure, compute backoff (respecting `Retry-After`), sleep, retry.
/// 5. On reqwest transport error: record failure, retry if retryable.
/// 6. After all retries exhausted: return `AllRetriesExhausted`.
pub async fn execute_with_retry<F, Fut>(
    config: &RetryConfig,
    circuit: &CircuitBreakerRegistry,
    circuit_key: &str,
    make_request: F,
) -> Result<reqwest::Response, RetryError>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<reqwest::Response, reqwest::Error>>,
{
    // Back-compat shim for callers that don't yet pass a retry budget.
    // Forwards to the budget-aware impl with `None` so no budget check
    // fires — behaviour stays identical.
    execute_with_retry_and_budget(config, circuit, circuit_key, None, 0, make_request).await
}

/// Budget-aware retry. `retry_budget` is an optional cap on how many
/// retries may be in flight for `circuit_key` at once — see `RetryBudget`
/// docs. Pass `None` to disable the check (identical to the legacy
/// `execute_with_retry`). `active_requests` should come from the caller's
/// router state: `budget * active_requests * ratio` determines the cap.
pub async fn execute_with_retry_and_budget<F, Fut>(
    config: &RetryConfig,
    circuit: &CircuitBreakerRegistry,
    circuit_key: &str,
    retry_budget: Option<&RetryBudget>,
    active_requests: i64,
    mut make_request: F,
) -> Result<reqwest::Response, RetryError>
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = Result<reqwest::Response, reqwest::Error>>,
{
    // 1. Check circuit breaker before any attempt.
    if let Err(err) = circuit.check_allowed(circuit_key) {
        tracing::debug!(
            circuit_key = circuit_key,
            remaining_secs = err.remaining.as_secs_f64(),
            "Circuit breaker open — rejecting request"
        );
        return Err(RetryError::CircuitOpen(err.to_string()));
    }

    let total_attempts = config.max_retries + 1;
    let mut last_error_msg = String::new();

    // `retry_slot_held` tracks whether the last attempt was granted a
    // budget slot so we can `release()` it in every exit path. The slot
    // is acquired *before* performing a retry attempt and released
    // *after* the attempt completes — on both success and failure.
    let mut retry_slot_held = false;

    for attempt in 0..total_attempts {
        // For any attempt after the first, consult the retry budget.
        if attempt > 0 {
            if let Some(budget) = retry_budget {
                if !budget.try_acquire(circuit_key, active_requests) {
                    tracing::warn!(
                        circuit_key = circuit_key,
                        attempt,
                        active_retries = budget.active(circuit_key),
                        active_requests,
                        "Retry budget exhausted — refusing further attempts"
                    );
                    return Err(RetryError::BudgetExhausted {
                        last_error: last_error_msg,
                        active_retries: budget.active(circuit_key),
                    });
                }
                retry_slot_held = true;
            }
        }

        let result = make_request().await;

        // Always release the slot before handling the result so early-
        // return paths below (Ok, NonRetryable, etc.) don't leak slots.
        if retry_slot_held {
            if let Some(budget) = retry_budget {
                budget.release(circuit_key);
            }
            retry_slot_held = false;
        }

        match result {
            Ok(response) => {
                let status = response.status().as_u16();

                // A 5xx/429 always signals upstream trouble for the circuit
                // breaker, but whether we may RETRY it depends on idempotency
                // (a 502/504 could mean the side effect already ran).
                if !is_retryable_status(status) {
                    // Success or a definitive client error — the upstream is
                    // healthy, it just answered. Record success.
                    circuit.record_success(circuit_key);

                    if status >= 400 {
                        return Err(RetryError::NonRetryable(response));
                    }
                    return Ok(response);
                }

                // Retryable-class status — record failure for the breaker.
                let just_opened = circuit.record_failure(circuit_key);
                if just_opened {
                    tracing::warn!(
                        circuit_key = circuit_key,
                        status = status,
                        "Circuit breaker opened after retryable status"
                    );
                }

                last_error_msg = format!("HTTP {status}");

                // For a non-idempotent request, an ambiguous 502/503/504 must
                // NOT be retried — surface it so we don't risk re-running a
                // side effect. (429 stays retryable: rejected before
                // processing.)
                if !status_retryable_for(status, config.idempotent) {
                    tracing::warn!(
                        circuit_key = circuit_key,
                        status = status,
                        "Not retrying non-idempotent request after ambiguous upstream status"
                    );
                    // Not retried, and not silently "failed" either:
                    // the side effect may have run.
                    crate::services::evidence::request::record_ambiguous_upstream(&format!(
                        "HTTP {status} after the request was sent"
                    ));
                    return Err(RetryError::NonRetryable(response));
                }

                if attempt < total_attempts - 1 {
                    let backoff = compute_backoff(config, attempt, response.headers());
                    if config.waits_past_deadline(backoff) {
                        return Err(RetryError::AllRetriesExhausted {
                            last_error: last_error_msg,
                            attempts: attempt + 1,
                        });
                    }
                    tracing::debug!(
                        circuit_key = circuit_key,
                        attempt = attempt + 1,
                        max_attempts = total_attempts,
                        backoff_ms = backoff.as_millis() as u64,
                        status = status,
                        "Retrying after retryable status"
                    );
                    tokio::time::sleep(backoff).await;
                }
            }
            Err(err) => {
                // Idempotency-aware: a non-idempotent request retries only on
                // a connection error (provably never reached the upstream).
                // A timeout is ambiguous — the tool may have already run — so
                // it is not retried for `tools/call` and surfaces instead.
                let retryable = error_retryable_for(&err, config.idempotent);
                let just_opened = circuit.record_failure(circuit_key);
                if just_opened {
                    tracing::warn!(
                        circuit_key = circuit_key,
                        error = %err,
                        "Circuit breaker opened after transport error"
                    );
                }

                last_error_msg = err.to_string();

                if retryable && attempt < total_attempts - 1 {
                    let backoff = compute_backoff(config, attempt, &HeaderMap::new());
                    if config.waits_past_deadline(backoff) {
                        return Err(RetryError::AllRetriesExhausted {
                            last_error: last_error_msg,
                            attempts: attempt + 1,
                        });
                    }
                    tracing::debug!(
                        circuit_key = circuit_key,
                        attempt = attempt + 1,
                        max_attempts = total_attempts,
                        backoff_ms = backoff.as_millis() as u64,
                        error = %err,
                        "Retrying after transport error"
                    );
                    tokio::time::sleep(backoff).await;
                } else if !retryable {
                    // A non-idempotent request that may have reached
                    // the upstream (anything but a refused connection):
                    // its outcome is unknown.
                    if !config.idempotent && !err.is_connect() {
                        crate::services::evidence::request::record_ambiguous_upstream(
                            if err.is_timeout() {
                                "timeout after the request was sent"
                            } else {
                                "transport error after the request was sent"
                            },
                        );
                    }
                    // Non-retryable transport error — bail immediately.
                    return Err(RetryError::AllRetriesExhausted {
                        last_error: last_error_msg,
                        attempts: attempt + 1,
                    });
                }
            }
        }
    }

    Err(RetryError::AllRetriesExhausted {
        last_error: last_error_msg,
        attempts: total_attempts,
    })
}

/// Compute the backoff duration for a given retry attempt.
///
/// Uses **full jitter** exponential backoff:
///     `sleep = random(0, min(cap, base * factor^attempt))`
///
/// Full jitter (vs. deterministic `base * factor^attempt` or bounded
/// `half+jitter`) best protects upstreams from coordinated thundering herds:
/// every retry picks an independent uniform-random delay in `[0, cap]`, so a
/// synchronised outage + recovery doesn't cause every client's retry to
/// arrive at the same millisecond.
///
/// If a `Retry-After` header is present and longer than the computed delay,
/// the header wins (providers set it deliberately). Caps at 30 seconds to
/// avoid unbounded waits on deep failures.
fn compute_backoff(config: &RetryConfig, attempt: u32, headers: &HeaderMap) -> Duration {
    // Saturating power: an `attempt` large enough to overflow is clamped to
    // the 30-second cap below rather than panicking.
    let exponential = config
        .base_delay
        .saturating_mul(config.backoff_factor.saturating_pow(attempt));
    let cap = Duration::from_secs(30);
    let upper = exponential.min(cap);

    // Full-jitter: uniform in [0, upper].
    let upper_ms = upper.as_millis() as u64;
    let jittered = if upper_ms == 0 {
        Duration::ZERO
    } else {
        let sleep_ms = rand::rng().random_range(0..=upper_ms);
        Duration::from_millis(sleep_ms)
    };

    // `Retry-After` always wins when present — providers set it
    // intentionally (e.g. rate-limit reset times) and we must honour.
    let retry_after = extract_retry_after(headers).unwrap_or(Duration::ZERO);
    jittered.max(retry_after).min(cap)
}

#[cfg(test)]
mod budget_and_jitter_tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};

    // ── RetryBudget ───────────────────────────────────────────────────

    #[test]
    fn budget_allows_min_concurrent_when_in_flight_is_zero() {
        // Cold start: even with zero in-flight, the budget should
        // still permit a minimum number of retries — otherwise the
        // very first user to hit a blip gets zero retry headroom.
        let budget = RetryBudget::new().with_min_concurrent(3).with_ratio(0.2);
        assert!(budget.try_acquire("m1", 0));
        assert!(budget.try_acquire("m1", 0));
        assert!(budget.try_acquire("m1", 0));
        // 4th should be over the min floor.
        assert!(!budget.try_acquire("m1", 0));
    }

    #[test]
    fn budget_scales_with_in_flight_via_ratio() {
        let budget = RetryBudget::new().with_min_concurrent(1).with_ratio(0.5);
        // 10 in-flight × 0.5 = 5 allowed. Ceil kicks in on fractional
        // upsets, so we go up to 5 and the 6th is rejected.
        for _ in 0..5 {
            assert!(budget.try_acquire("m1", 10));
        }
        assert!(!budget.try_acquire("m1", 10));
    }

    #[test]
    fn budget_release_frees_a_slot() {
        let budget = RetryBudget::new().with_min_concurrent(2).with_ratio(0.2);
        assert!(budget.try_acquire("m1", 0));
        assert!(budget.try_acquire("m1", 0));
        assert!(!budget.try_acquire("m1", 0)); // over min
        budget.release("m1");
        // Now one slot is free.
        assert!(budget.try_acquire("m1", 0));
        // But a second over-the-limit call still fails.
        assert!(!budget.try_acquire("m1", 0));
    }

    #[test]
    fn budget_is_per_key() {
        let budget = RetryBudget::new().with_min_concurrent(1).with_ratio(0.0);
        assert!(budget.try_acquire("a", 0));
        assert!(!budget.try_acquire("a", 0));
        // Different key shares no counter.
        assert!(budget.try_acquire("b", 0));
        assert!(!budget.try_acquire("b", 0));
    }

    #[test]
    fn budget_rolls_back_on_rejection() {
        // The optimistic-increment-then-rollback pattern in
        // try_acquire must not leave the counter above the ceiling
        // once rejection has returned.
        let budget = RetryBudget::new().with_min_concurrent(1).with_ratio(0.0);
        assert!(budget.try_acquire("m1", 0));
        assert!(!budget.try_acquire("m1", 0)); // rejected
        assert_eq!(
            budget.active("m1"),
            1,
            "rollback should leave count at 1, not 2"
        );
    }

    // ── Full-jitter backoff ────────────────────────────────────────────

    #[test]
    fn jittered_backoff_stays_inside_exponential_cap() {
        // A large sample: every draw must lie within
        // [0, base * factor^attempt], bounded by the 30s cap.
        let config = RetryConfig::default();
        for attempt in 0..5 {
            let upper_ms = (config.base_delay * config.backoff_factor.saturating_pow(attempt))
                .min(Duration::from_secs(30))
                .as_millis() as u64;
            for _ in 0..200 {
                let b = compute_backoff(&config, attempt, &HeaderMap::new());
                assert!(
                    b.as_millis() as u64 <= upper_ms,
                    "attempt={attempt}: backoff {}ms exceeded cap {upper_ms}ms",
                    b.as_millis(),
                );
            }
        }
    }

    #[test]
    fn jittered_backoff_actually_varies() {
        // If jitter is broken (e.g. RNG always returns 0), this will
        // collapse. 100 samples at attempt=3 should produce well
        // over 5 distinct values with 25.6 seconds of upper range.
        let config = RetryConfig::default();
        let mut seen = std::collections::HashSet::new();
        for _ in 0..100 {
            let b = compute_backoff(&config, 3, &HeaderMap::new());
            seen.insert(b.as_millis() as u64);
        }
        assert!(
            seen.len() > 5,
            "jitter should produce diverse samples; only saw {} distinct values",
            seen.len(),
        );
    }

    #[test]
    fn retry_after_header_still_wins() {
        // Even with jitter, a long Retry-After hint must override a
        // (randomly small) jittered value. Providers set it for a
        // reason — respecting it is non-negotiable.
        let config = RetryConfig {
            max_retries: 2,
            base_delay: Duration::from_millis(10),
            backoff_factor: 2,
            idempotent: true,
            deadline: None,
        };
        let mut headers = HeaderMap::new();
        headers.insert("retry-after", "5".parse().unwrap());
        // Run many draws — every one must be ≥ 5s.
        for _ in 0..50 {
            let b = compute_backoff(&config, 0, &headers);
            assert!(
                b >= Duration::from_secs(5),
                "Retry-After should dominate jitter; got {}ms",
                b.as_millis(),
            );
        }
    }

    // ── Budget integration with execute_with_retry_and_budget ─────────

    struct CountingMockClient {
        call_count: AtomicU32,
    }

    impl CountingMockClient {
        fn new() -> Self {
            Self {
                call_count: AtomicU32::new(0),
            }
        }
    }

    #[tokio::test]
    async fn budget_exhaustion_short_circuits_retry() {
        use crate::services::circuit_breaker::CircuitBreakerRegistry;

        let registry = CircuitBreakerRegistry::new();
        let budget = RetryBudget::new().with_min_concurrent(0).with_ratio(0.0);
        let client = CountingMockClient::new();

        let config = RetryConfig {
            max_retries: 5,
            base_delay: Duration::from_millis(1),
            backoff_factor: 2,
            idempotent: true,
            deadline: None,
        };
        let result = execute_with_retry_and_budget(
            &config,
            &registry,
            "m1",
            Some(&budget),
            0, // no in-flight → budget is 0 (min=0, ratio=0)
            || {
                client.call_count.fetch_add(1, Ordering::Relaxed);
                async {
                    // Simulate a 502 every call. Note: we can't easily
                    // construct a `reqwest::Response` in unit tests
                    // without a real HTTP server, so we simulate the
                    // transport-error path instead.
                    // Cheap stand-in: connect-refused to an unreachable host.
                    reqwest::Client::new()
                        .get("http://127.0.0.1:1/unreachable")
                        .timeout(Duration::from_millis(50))
                        .send()
                        .await
                }
            },
        )
        .await;

        // With budget=0 and a failing first request, the first call
        // goes through (attempt 0 doesn't consult the budget), but
        // the retry is blocked.
        assert!(
            matches!(result, Err(RetryError::BudgetExhausted { .. })),
            "expected BudgetExhausted, got {result:?}",
        );
        assert_eq!(
            client.call_count.load(Ordering::Relaxed),
            1,
            "budget should prevent a second attempt",
        );
    }

    // ── Idempotency-aware retry (non-idempotent tools/call) ───────────

    #[test]
    fn status_retryable_respects_idempotency() {
        // Idempotent: full transient set retries.
        for s in [429u16, 502, 503, 504] {
            assert!(status_retryable_for(s, true), "idempotent should retry {s}");
        }
        // Non-idempotent: only 429 (rejected before processing) retries; an
        // ambiguous 502/503/504 must not (the side effect may have run).
        assert!(status_retryable_for(429, false));
        for s in [502u16, 503, 504] {
            assert!(
                !status_retryable_for(s, false),
                "non-idempotent must not retry {s}"
            );
        }
        // Success / definitive client errors never retry, either way.
        for s in [200u16, 400, 401, 404] {
            assert!(!status_retryable_for(s, true));
            assert!(!status_retryable_for(s, false));
        }
    }

    #[tokio::test]
    async fn timeout_error_retry_depends_on_idempotency() {
        // A zero-duration timeout against a non-routable IP yields a reqwest
        // *timeout* error with no DNS/connect race — the same construction the
        // MCP route uses to surface its tokio timeout.
        let timeout_err = reqwest::Client::new()
            .get("http://192.0.2.0/")
            .timeout(Duration::ZERO)
            .send()
            .await
            .expect_err("zero timeout must error");
        assert!(
            timeout_err.is_timeout(),
            "sentinel must classify as timeout"
        );
        assert!(
            !timeout_err.is_connect(),
            "sentinel must NOT be a connect error"
        );
        assert!(
            error_retryable_for(&timeout_err, true),
            "idempotent requests retry timeouts"
        );
        assert!(
            !error_retryable_for(&timeout_err, false),
            "non-idempotent requests must NOT retry timeouts"
        );
    }

    #[tokio::test]
    async fn non_idempotent_request_runs_exactly_once_on_timeout() {
        use crate::services::circuit_breaker::CircuitBreakerRegistry;
        let registry = CircuitBreakerRegistry::new();
        let client = CountingMockClient::new();
        let config = RetryConfig {
            max_retries: 3,
            base_delay: Duration::from_millis(1),
            backoff_factor: 2,
            idempotent: false,
            deadline: None,
        };
        let result = execute_with_retry_and_budget(&config, &registry, "srv1", None, 0, || {
            client.call_count.fetch_add(1, Ordering::Relaxed);
            async {
                reqwest::Client::new()
                    .get("http://192.0.2.0/")
                    .timeout(Duration::ZERO)
                    .send()
                    .await
            }
        })
        .await;
        assert!(matches!(
            result,
            Err(RetryError::AllRetriesExhausted { .. })
        ));
        assert_eq!(
            client.call_count.load(Ordering::Relaxed),
            1,
            "a non-idempotent tools/call must execute exactly once on timeout",
        );
    }

    #[tokio::test]
    async fn idempotent_request_retries_on_timeout() {
        use crate::services::circuit_breaker::CircuitBreakerRegistry;
        let registry = CircuitBreakerRegistry::new();
        let client = CountingMockClient::new();
        let config = RetryConfig {
            max_retries: 2,
            base_delay: Duration::from_millis(1),
            backoff_factor: 2,
            idempotent: true,
            deadline: None,
        };
        let _ = execute_with_retry_and_budget(&config, &registry, "srv2", None, 0, || {
            client.call_count.fetch_add(1, Ordering::Relaxed);
            async {
                reqwest::Client::new()
                    .get("http://192.0.2.0/")
                    .timeout(Duration::ZERO)
                    .send()
                    .await
            }
        })
        .await;
        assert_eq!(
            client.call_count.load(Ordering::Relaxed),
            3,
            "idempotent request should attempt 1 + 2 retries on timeout",
        );
    }
}

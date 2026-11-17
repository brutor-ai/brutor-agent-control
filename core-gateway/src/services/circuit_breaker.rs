// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Circuit Breaker
//!
//! Per-MCP-server circuit breaker with a DashMap-based registry.
//! Prevents cascading failures by short-circuiting requests to
//! unhealthy upstream servers.
//!
//! Uses `std::sync::Mutex` (not tokio) because locks are never
//! held across `.await` points. DashMap provides shard-level
//! concurrency so contention is minimal even under high load.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use dashmap::DashMap;

/// The three states of a circuit breaker.
#[derive(Debug, Clone, PartialEq)]
pub enum CircuitState {
    /// Normal operation — requests flow through.
    Closed,
    /// Requests are blocked. Contains the instant the circuit opened.
    Open { opened_at: Instant },
    /// A single probe request is allowed to test recovery.
    HalfOpen,
}

/// Per-server circuit breaker tracking consecutive failures and state transitions.
pub struct CircuitBreaker {
    state: CircuitState,
    consecutive_failures: u32,
    last_activity: Instant,
    failure_threshold: u32,
    recovery_timeout: Duration,
}

impl CircuitBreaker {
    fn new(failure_threshold: u32, recovery_timeout: Duration) -> Self {
        Self {
            state: CircuitState::Closed,
            consecutive_failures: 0,
            last_activity: Instant::now(),
            failure_threshold,
            recovery_timeout,
        }
    }
}

/// Thread-safe registry of circuit breakers keyed by server identifier.
///
/// Breakers are created lazily on first access and cleaned up when idle
/// for more than 5 minutes. The registry is designed to be stored in
/// `AppState` and shared across all request handlers.
pub struct CircuitBreakerRegistry {
    breakers: DashMap<String, Mutex<CircuitBreaker>>,
    failure_threshold: u32,
    recovery_timeout: Duration,
}

impl CircuitBreakerRegistry {
    /// Create a new registry with default settings (threshold=5, recovery=30s).
    pub fn new() -> Self {
        Self {
            breakers: DashMap::new(),
            failure_threshold: 5,
            recovery_timeout: Duration::from_secs(30),
        }
    }

    /// Configure per-server thresholds. Creates the breaker if it doesn't exist,
    /// or updates the thresholds if it does (without resetting state).
    pub fn configure(&self, key: &str, failure_threshold: u32, recovery_timeout: Duration) {
        let entry = self.breakers.entry(key.to_string()).or_insert_with(|| {
            Mutex::new(CircuitBreaker::new(failure_threshold, recovery_timeout))
        });
        let mut breaker = entry.value().lock().unwrap();
        breaker.failure_threshold = failure_threshold;
        breaker.recovery_timeout = recovery_timeout;
    }

    /// Check if a request to the given server is allowed.
    ///
    /// Returns `Ok(())` if the circuit is closed or half-open (probe allowed).
    /// Returns `Err(CircuitOpenError)` with the remaining cooldown duration
    /// if the circuit is open and the recovery timeout has not yet elapsed.
    ///
    /// Automatically transitions `Open` to `HalfOpen` when the recovery
    /// timeout expires, allowing a single probe request through.
    pub fn check_allowed(&self, key: &str) -> Result<(), CircuitOpenError> {
        let entry = self.breakers.entry(key.to_string()).or_insert_with(|| {
            Mutex::new(CircuitBreaker::new(
                self.failure_threshold,
                self.recovery_timeout,
            ))
        });

        let mut breaker = entry.value().lock().unwrap();
        breaker.last_activity = Instant::now();

        match &breaker.state {
            CircuitState::Closed | CircuitState::HalfOpen => Ok(()),
            CircuitState::Open { opened_at } => {
                let elapsed = opened_at.elapsed();
                if elapsed >= breaker.recovery_timeout {
                    tracing::debug!(
                        circuit_key = key,
                        "Circuit transitioning Open -> HalfOpen (recovery timeout elapsed)"
                    );
                    breaker.state = CircuitState::HalfOpen;
                    Ok(())
                } else {
                    let remaining = breaker.recovery_timeout - elapsed;
                    Err(CircuitOpenError { remaining })
                }
            }
        }
    }

    /// Record a successful request for the given server.
    ///
    /// Transitions `HalfOpen` back to `Closed` and resets the failure
    /// counter. For `Closed` state, simply resets the failure counter.
    pub fn record_success(&self, key: &str) {
        if let Some(entry) = self.breakers.get(key) {
            let mut breaker = entry.value().lock().unwrap();
            breaker.last_activity = Instant::now();

            match breaker.state {
                CircuitState::HalfOpen => {
                    tracing::debug!(
                        circuit_key = key,
                        "Circuit transitioning HalfOpen -> Closed (probe succeeded)"
                    );
                    breaker.state = CircuitState::Closed;
                    breaker.consecutive_failures = 0;
                }
                CircuitState::Closed => {
                    breaker.consecutive_failures = 0;
                }
                CircuitState::Open { .. } => {
                    // Shouldn't happen — success while open means a request leaked through.
                    // Reset anyway for safety.
                    tracing::warn!(
                        circuit_key = key,
                        "Unexpected success while circuit is Open — resetting to Closed"
                    );
                    breaker.state = CircuitState::Closed;
                    breaker.consecutive_failures = 0;
                }
            }
        }
    }

    /// Record a failed request for the given server.
    ///
    /// Increments the consecutive failure counter. If the threshold is
    /// reached in `Closed` state, transitions to `Open`. In `HalfOpen`
    /// state, immediately transitions back to `Open` (probe failed).
    ///
    /// Returns `true` if the circuit just opened (useful for triggering
    /// a health status update).
    pub fn record_failure(&self, key: &str) -> bool {
        let entry = self.breakers.entry(key.to_string()).or_insert_with(|| {
            Mutex::new(CircuitBreaker::new(
                self.failure_threshold,
                self.recovery_timeout,
            ))
        });

        let mut breaker = entry.value().lock().unwrap();
        breaker.last_activity = Instant::now();
        breaker.consecutive_failures += 1;

        match breaker.state {
            CircuitState::Closed => {
                if breaker.consecutive_failures >= breaker.failure_threshold {
                    tracing::warn!(
                        circuit_key = key,
                        failures = breaker.consecutive_failures,
                        threshold = breaker.failure_threshold,
                        "Circuit transitioning Closed -> Open (failure threshold reached)"
                    );
                    breaker.state = CircuitState::Open {
                        opened_at: Instant::now(),
                    };
                    true
                } else {
                    false
                }
            }
            CircuitState::HalfOpen => {
                tracing::warn!(
                    circuit_key = key,
                    "Circuit transitioning HalfOpen -> Open (probe failed)"
                );
                breaker.state = CircuitState::Open {
                    opened_at: Instant::now(),
                };
                true
            }
            CircuitState::Open { .. } => {
                // Already open — no transition.
                false
            }
        }
    }

    /// Remove circuit breaker entries that have been idle for more than 5 minutes.
    ///
    /// Intended to be called from a periodic cleanup loop (e.g. every 60s)
    /// to prevent unbounded memory growth from ephemeral server keys.
    pub fn cleanup(&self) {
        let idle_threshold = Duration::from_secs(300);
        self.breakers.retain(|key, mutex| {
            let breaker = mutex.get_mut().unwrap();
            let idle = breaker.last_activity.elapsed() < idle_threshold;
            if !idle {
                tracing::debug!(circuit_key = %key, "Removing idle circuit breaker entry");
            }
            idle
        });
    }

    /// Get the current circuit state for a given key (for metrics/logging).
    ///
    /// Returns `None` if no breaker exists for the key.
    pub fn get_state(&self, key: &str) -> Option<CircuitState> {
        self.breakers
            .get(key)
            .map(|entry| entry.value().lock().unwrap().state.clone())
    }
}

/// Error returned when a circuit breaker is open and blocking requests.
#[derive(Debug)]
pub struct CircuitOpenError {
    /// How much time remains before the circuit transitions to half-open.
    pub remaining: Duration,
}

impl std::fmt::Display for CircuitOpenError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "Circuit breaker is open — retry after {:.1}s",
            self.remaining.as_secs_f64()
        )
    }
}

impl std::error::Error for CircuitOpenError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_starts_closed_and_allows_requests() {
        let registry = CircuitBreakerRegistry::new();
        assert!(registry.check_allowed("server-1").is_ok());
    }

    #[test]
    fn test_stays_closed_below_threshold() {
        let registry = CircuitBreakerRegistry::new(); // threshold=5
        for _ in 0..4 {
            let opened = registry.record_failure("server-1");
            assert!(!opened, "Should not open below threshold");
        }
        assert!(
            registry.check_allowed("server-1").is_ok(),
            "Should still be closed"
        );
    }

    #[test]
    fn test_opens_at_threshold() {
        let registry = CircuitBreakerRegistry::new(); // threshold=5
        for i in 0..5 {
            let opened = registry.record_failure("server-1");
            if i < 4 {
                assert!(!opened);
            } else {
                assert!(opened, "Should open on 5th failure");
            }
        }
        assert!(
            registry.check_allowed("server-1").is_err(),
            "Should be open"
        );
    }

    #[test]
    fn test_success_resets_failures() {
        let registry = CircuitBreakerRegistry::new();
        for _ in 0..4 {
            registry.record_failure("server-1");
        }
        registry.record_success("server-1");
        // After success, failures reset. Need 5 more to open.
        for _ in 0..4 {
            registry.record_failure("server-1");
        }
        assert!(
            registry.check_allowed("server-1").is_ok(),
            "Should still be closed after reset"
        );
    }

    #[test]
    fn test_open_transitions_to_half_open_after_recovery() {
        let registry = CircuitBreakerRegistry {
            breakers: DashMap::new(),
            failure_threshold: 2,
            recovery_timeout: Duration::from_millis(10), // very short for testing
        };

        registry.record_failure("s1");
        registry.record_failure("s1"); // opens
        assert!(registry.check_allowed("s1").is_err());

        // Wait for recovery timeout
        std::thread::sleep(Duration::from_millis(15));

        // Should transition to half-open, allowing one probe
        assert!(
            registry.check_allowed("s1").is_ok(),
            "Should be half-open after recovery"
        );
    }

    #[test]
    fn test_half_open_success_closes() {
        let registry = CircuitBreakerRegistry {
            breakers: DashMap::new(),
            failure_threshold: 2,
            recovery_timeout: Duration::from_millis(10),
        };

        registry.record_failure("s1");
        registry.record_failure("s1"); // opens
        std::thread::sleep(Duration::from_millis(15)); // wait for half-open

        registry.check_allowed("s1").unwrap(); // transition to half-open
        registry.record_success("s1"); // should close

        assert!(
            registry.check_allowed("s1").is_ok(),
            "Should be closed after half-open success"
        );
        assert_eq!(registry.get_state("s1"), Some(CircuitState::Closed));
    }

    #[test]
    fn test_half_open_failure_reopens() {
        let registry = CircuitBreakerRegistry {
            breakers: DashMap::new(),
            failure_threshold: 2,
            recovery_timeout: Duration::from_millis(10),
        };

        registry.record_failure("s1");
        registry.record_failure("s1"); // opens
        std::thread::sleep(Duration::from_millis(15));

        registry.check_allowed("s1").unwrap(); // half-open
        let opened = registry.record_failure("s1"); // probe fails → re-open
        assert!(opened, "Should re-open on half-open failure");
        assert!(
            registry.check_allowed("s1").is_err(),
            "Should be open again"
        );
    }

    #[test]
    fn test_configure_updates_thresholds() {
        let registry = CircuitBreakerRegistry::new();
        registry.configure("s1", 3, Duration::from_secs(10));

        // Should now open after 3 failures (not default 5)
        for _ in 0..2 {
            registry.record_failure("s1");
        }
        assert!(registry.check_allowed("s1").is_ok());
        registry.record_failure("s1"); // 3rd failure
        assert!(
            registry.check_allowed("s1").is_err(),
            "Should open at custom threshold of 3"
        );
    }

    #[test]
    fn test_cleanup_removes_idle_entries() {
        let registry = CircuitBreakerRegistry::new();
        registry.record_success("s1");

        // Manually set last_activity to >5 minutes ago
        if let Some(entry) = registry.breakers.get("s1") {
            let mut breaker = entry.value().lock().unwrap();
            breaker.last_activity = Instant::now() - Duration::from_secs(600);
        }

        registry.cleanup();
        assert_eq!(
            registry.get_state("s1"),
            None,
            "Should have been cleaned up"
        );
    }

    #[test]
    fn test_separate_keys_are_independent() {
        let registry = CircuitBreakerRegistry::new();
        for _ in 0..5 {
            registry.record_failure("s1");
        }
        assert!(registry.check_allowed("s1").is_err());
        assert!(
            registry.check_allowed("s2").is_ok(),
            "Different key should be unaffected"
        );
    }

    #[test]
    fn test_circuit_open_error_has_remaining_duration() {
        let registry = CircuitBreakerRegistry {
            breakers: DashMap::new(),
            failure_threshold: 1,
            recovery_timeout: Duration::from_secs(30),
        };
        registry.record_failure("s1"); // opens immediately
        let err = registry.check_allowed("s1").unwrap_err();
        assert!(err.remaining.as_secs() > 0 && err.remaining.as_secs() <= 30);
    }
}

#[cfg(test)]
mod retry_tests {
    use super::*;
    use crate::services::retry::{self, RetryConfig, RetryError};

    #[tokio::test]
    async fn test_is_retryable_status() {
        assert!(retry::is_retryable_status(429));
        assert!(retry::is_retryable_status(502));
        assert!(retry::is_retryable_status(503));
        assert!(retry::is_retryable_status(504));
        assert!(!retry::is_retryable_status(200));
        assert!(!retry::is_retryable_status(400));
        assert!(!retry::is_retryable_status(401));
        assert!(!retry::is_retryable_status(404));
        assert!(!retry::is_retryable_status(500));
    }

    #[tokio::test]
    async fn test_extract_retry_after_seconds() {
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert("retry-after", "5".parse().unwrap());
        let duration = retry::extract_retry_after(&headers);
        assert_eq!(duration, Some(Duration::from_secs(5)));
    }

    #[tokio::test]
    async fn test_extract_retry_after_missing() {
        let headers = reqwest::header::HeaderMap::new();
        assert_eq!(retry::extract_retry_after(&headers), None);
    }

    #[tokio::test]
    async fn test_circuit_open_prevents_request() {
        let registry = CircuitBreakerRegistry {
            breakers: DashMap::new(),
            failure_threshold: 1,
            recovery_timeout: Duration::from_secs(60),
        };
        registry.record_failure("s1"); // opens

        let config = RetryConfig::default();
        let result = retry::execute_with_retry(&config, &registry, "s1", || async {
            unreachable!("Should not attempt request")
        })
        .await;

        assert!(matches!(result, Err(RetryError::CircuitOpen(_))));
    }

    #[tokio::test]
    async fn test_retry_config_defaults() {
        let config = RetryConfig::default();
        assert_eq!(config.max_retries, 2);
        assert_eq!(config.base_delay, Duration::from_millis(100));
        assert_eq!(config.backoff_factor, 4);
    }
}

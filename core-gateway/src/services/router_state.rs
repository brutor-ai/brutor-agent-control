// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
// Router state — in-memory tracking for cooldowns, rate limiting,
// latency, and concurrency. Thread-safe via DashMap.
//
// Redis backing for multi-pod deployments:
// - Cooldowns, RPM, TPM, and group concurrency write to both DashMap
//   and Redis (fire-and-forget) when Redis is available.
// - Reads always use DashMap (fast local path). Cross-pod consistency
//   comes from each pod writing its own state to Redis.
// - RPM/TPM use Redis INCRBY so counters aggregate across pods.
// - Cooldowns use Redis SET with EX so they propagate across pods.
// - Group concurrency uses Redis INCR/DECR for cross-pod slot tracking.
// - Latency, response cache, and tool call counters stay per-pod only.

use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use dashmap::DashMap;

/// Global router state, stored in AppState.
///
/// When Redis is configured, critical counters (RPM, TPM, cooldowns,
/// group concurrency) are also written to Redis for cross-pod consistency.
/// All reads go through the local DashMap for zero-latency access.
pub struct RouterState {
    /// Per-model cooldown tracking
    cooldowns: DashMap<String, CooldownEntry>,
    /// Per-model RPM counters (key = "model_id:minute_key")
    rpm_counters: DashMap<String, AtomicI64>,
    /// Per-model TPM counters (key = "model_id:minute_key")
    tpm_counters: DashMap<String, AtomicI64>,
    /// Per-model latency tracking (exponential moving average)
    latency_tracker: DashMap<String, LatencyEntry>,
    /// Per-model (or group) active request count for least-busy routing
    active_requests: DashMap<String, AtomicI64>,
    /// Optional Redis connection for cross-pod state sharing
    redis: Option<redis::aio::ConnectionManager>,
}

#[derive(Debug, Clone)]
#[allow(dead_code)]
struct CooldownEntry {
    /// When the cooldown was set
    started_at: Instant,
    /// How long the cooldown lasts
    duration: Duration,
    /// The error that triggered the cooldown
    reason: String,
    /// HTTP status code that triggered it
    status_code: u16,
}

#[derive(Debug)]
struct LatencyEntry {
    /// Exponential moving average in ms
    avg_ms: std::sync::atomic::AtomicU64,
    /// Number of samples
    count: AtomicU64,
}

impl RouterState {
    pub fn new(redis: Option<redis::aio::ConnectionManager>) -> Self {
        if redis.is_none() {
            tracing::warn!(
                "Redis not configured for RouterState — rate limits and cooldowns are per-pod only. \
                 Multi-pod deployments require Redis for consistent rate limiting."
            );
        }
        Self {
            cooldowns: DashMap::new(),
            rpm_counters: DashMap::new(),
            tpm_counters: DashMap::new(),
            latency_tracker: DashMap::new(),
            active_requests: DashMap::new(),
            redis,
        }
    }

    // =========================================================================
    // Redis helpers — fire-and-forget writes for cross-pod consistency
    // =========================================================================

    /// Spawn a fire-and-forget Redis command. Errors are logged but don't
    /// affect the caller — DashMap is always the primary store.
    fn redis_fire_and_forget(&self, f: impl std::future::Future<Output = ()> + Send + 'static) {
        tokio::spawn(f);
    }

    /// SET a Redis key with TTL (fire-and-forget).
    fn redis_set_ex(&self, key: String, value: String, ttl_secs: u64) {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            self.redis_fire_and_forget(async move {
                let result: Result<(), redis::RedisError> = redis::cmd("SET")
                    .arg(&key)
                    .arg(&value)
                    .arg("EX")
                    .arg(ttl_secs as i64)
                    .query_async(&mut conn)
                    .await;
                if let Err(e) = result {
                    tracing::debug!(key = %key, error = %e, "Redis SET failed (fire-and-forget)");
                }
            });
        }
    }

    /// DEL a Redis key (fire-and-forget).
    fn redis_del(&self, key: String) {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            self.redis_fire_and_forget(async move {
                let result: Result<(), redis::RedisError> =
                    redis::cmd("DEL").arg(&key).query_async(&mut conn).await;
                if let Err(e) = result {
                    tracing::debug!(key = %key, error = %e, "Redis DEL failed (fire-and-forget)");
                }
            });
        }
    }

    /// INCR a Redis key with TTL (fire-and-forget). Used for RPM counters.
    fn redis_incr_ex(&self, key: String, ttl_secs: u64) {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            self.redis_fire_and_forget(async move {
                let result: Result<(), redis::RedisError> = redis::pipe()
                    .atomic()
                    .incr(&key, 1i64)
                    .expire(&key, ttl_secs as i64)
                    .ignore()
                    .query_async(&mut conn)
                    .await;
                if let Err(e) = result {
                    tracing::debug!(key = %key, error = %e, "Redis INCR failed (fire-and-forget)");
                }
            });
        }
    }

    /// INCRBY a Redis key with TTL (fire-and-forget). Used for TPM counters.
    fn redis_incrby_ex(&self, key: String, amount: i64, ttl_secs: u64) {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            self.redis_fire_and_forget(async move {
                let result: Result<(), redis::RedisError> = redis::pipe()
                    .atomic()
                    .incr(&key, amount)
                    .expire(&key, ttl_secs as i64)
                    .ignore()
                    .query_async(&mut conn)
                    .await;
                if let Err(e) = result {
                    tracing::debug!(key = %key, error = %e, "Redis INCRBY failed (fire-and-forget)");
                }
            });
        }
    }

    // =========================================================================
    // Cooldown tracking
    // =========================================================================

    /// Put a model into cooldown. It will be excluded from routing for `duration`.
    /// Also writes to Redis (if available) so other pods see the cooldown.
    pub fn set_cooldown(&self, model_id: &str, duration: Duration, status_code: u16, reason: &str) {
        tracing::info!(
            model_id,
            cooldown_secs = duration.as_secs(),
            status_code,
            reason,
            "Model entering cooldown"
        );
        self.cooldowns.insert(
            model_id.to_string(),
            CooldownEntry {
                started_at: Instant::now(),
                duration,
                reason: reason.to_string(),
                status_code,
            },
        );

        // Write cooldown to Redis with matching TTL
        let ttl_secs = duration.as_secs().max(1);
        let redis_key = format!("cooldown:{model_id}");
        self.redis_set_ex(redis_key, "1".to_string(), ttl_secs);
    }

    /// Check if a model is currently in cooldown.
    /// Reads from local DashMap only (fast path).
    pub fn is_cooled_down(&self, model_id: &str) -> bool {
        self.cooldown_remaining(model_id).is_some()
    }

    /// How long until a model leaves its cooldown, as this replica knows it;
    /// `None` when it is not cooling down. What a caller of a routing group
    /// with no member available is told to wait.
    /// Reads from local DashMap only.
    pub fn cooldown_remaining(&self, model_id: &str) -> Option<Duration> {
        if let Some(entry) = self.cooldowns.get(model_id) {
            if let Some(left) = entry.duration.checked_sub(entry.started_at.elapsed()) {
                if !left.is_zero() {
                    return Some(left);
                }
            }
            // Expired — remove it
            drop(entry);
            self.cooldowns.remove(model_id);
        }
        None
    }

    /// Seconds until the minute the per-model RPM and TPM counters are kept
    /// in ends (1 to 60): when a member that used up its rate counts anew.
    pub fn seconds_until_rate_window_ends() -> u64 {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        60 - now % 60
    }

    /// Async cooldown check that also consults Redis.
    /// Use this from async contexts where cross-pod consistency matters.
    pub async fn is_cooled_down_async(&self, model_id: &str) -> bool {
        // Check local first
        if self.is_cooled_down(model_id) {
            return true;
        }

        // Check Redis
        if let Some(ref redis) = self.redis {
            let redis_key = format!("cooldown:{model_id}");
            let mut conn = redis.clone();
            let result: Result<bool, _> = redis::cmd("EXISTS")
                .arg(&redis_key)
                .query_async(&mut conn)
                .await;

            if let Ok(true) = result {
                return true;
            }
        }

        false
    }

    /// Should this error trigger a cooldown?
    /// - 429 (rate limit) -> always cooldown
    /// - 401/403 (auth) -> always cooldown (broken key)
    /// - 408 (timeout) -> cooldown
    /// - 500+ (server error) -> cooldown
    /// - Repeated failures -> cooldown
    pub fn should_cooldown(status_code: u16) -> bool {
        matches!(status_code, 429 | 401 | 403 | 408 | 500..=599)
    }

    /// Record a failure and apply cooldown if warranted.
    pub fn record_failure(
        &self,
        model_id: &str,
        status_code: u16,
        cooldown_time_secs: i32,
        error_msg: &str,
    ) {
        if Self::should_cooldown(status_code) {
            let duration = Duration::from_secs(cooldown_time_secs.max(10) as u64);
            self.set_cooldown(model_id, duration, status_code, error_msg);
        }
    }

    /// Record a success — clears any cooldown (local + Redis).
    pub fn record_success(&self, model_id: &str) {
        self.cooldowns.remove(model_id);
        let redis_key = format!("cooldown:{model_id}");
        self.redis_del(redis_key);
    }

    // =========================================================================
    // RPM / TPM rate limiting (1-minute sliding windows)
    // =========================================================================

    fn current_minute_key() -> String {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        // minute-level granularity
        format!("{}", now / 60)
    }

    /// Increment RPM counter for a model. Returns the new count.
    /// Also increments the Redis counter (fire-and-forget) for cross-pod aggregation.
    pub fn increment_rpm(&self, model_id: &str) -> i64 {
        let key = format!("{}:rpm:{}", model_id, Self::current_minute_key());
        let counter = self
            .rpm_counters
            .entry(key.clone())
            .or_insert_with(|| AtomicI64::new(0));
        let local_val = counter.fetch_add(1, Ordering::Relaxed) + 1;

        // Fire-and-forget Redis INCR with 60s TTL
        let redis_key = format!("rpm:{key}");
        self.redis_incr_ex(redis_key, 60);

        local_val
    }

    /// Get current RPM for a model (local DashMap only).
    pub fn get_rpm(&self, model_id: &str) -> i64 {
        let key = format!("{}:rpm:{}", model_id, Self::current_minute_key());
        self.rpm_counters
            .get(&key)
            .map(|c| c.load(Ordering::Relaxed))
            .unwrap_or(0)
    }

    /// Check if a model has exceeded its RPM limit.
    pub fn check_rpm(&self, model_id: &str, limit: i32) -> bool {
        self.get_rpm(model_id) >= limit as i64
    }

    /// Async RPM check that consults Redis for cross-pod aggregate.
    /// Use this from async contexts where cross-pod consistency matters.
    pub async fn check_rpm_async(&self, model_id: &str, limit: i32) -> bool {
        // Check local first (fast path)
        if self.check_rpm(model_id, limit) {
            return true;
        }

        // Check Redis for cross-pod aggregate
        if let Some(ref redis) = self.redis {
            let key = format!("{}:rpm:{}", model_id, Self::current_minute_key());
            let redis_key = format!("rpm:{key}");
            let mut conn = redis.clone();
            let result: Result<i64, _> = redis::cmd("GET")
                .arg(&redis_key)
                .query_async(&mut conn)
                .await;

            if let Ok(val) = result {
                return val >= limit as i64;
            }
        }

        false
    }

    /// Increment TPM counter for a model by `tokens`. Returns the new count.
    /// Also increments the Redis counter (fire-and-forget) for cross-pod aggregation.
    pub fn add_tpm(&self, model_id: &str, tokens: i64) -> i64 {
        let key = format!("{}:tpm:{}", model_id, Self::current_minute_key());
        let counter = self
            .tpm_counters
            .entry(key.clone())
            .or_insert_with(|| AtomicI64::new(0));
        let local_val = counter.fetch_add(tokens, Ordering::Relaxed) + tokens;

        // Fire-and-forget Redis INCRBY with 60s TTL
        let redis_key = format!("tpm:{key}");
        self.redis_incrby_ex(redis_key, tokens, 60);

        local_val
    }

    /// Get current TPM for a model (local DashMap only).
    pub fn get_tpm(&self, model_id: &str) -> i64 {
        let key = format!("{}:tpm:{}", model_id, Self::current_minute_key());
        self.tpm_counters
            .get(&key)
            .map(|c| c.load(Ordering::Relaxed))
            .unwrap_or(0)
    }

    /// Check if a model has exceeded its TPM limit.
    pub fn check_tpm(&self, model_id: &str, limit: i32) -> bool {
        self.get_tpm(model_id) >= limit as i64
    }

    /// Async TPM check that consults Redis for cross-pod aggregate.
    pub async fn check_tpm_async(&self, model_id: &str, limit: i32) -> bool {
        if self.check_tpm(model_id, limit) {
            return true;
        }

        if let Some(ref redis) = self.redis {
            let key = format!("{}:tpm:{}", model_id, Self::current_minute_key());
            let redis_key = format!("tpm:{key}");
            let mut conn = redis.clone();
            let result: Result<i64, _> = redis::cmd("GET")
                .arg(&redis_key)
                .query_async(&mut conn)
                .await;

            if let Ok(val) = result {
                return val >= limit as i64;
            }
        }

        false
    }

    // =========================================================================
    // Latency tracking (exponential moving average) — per-pod only
    // =========================================================================

    /// Record a latency sample for a model.
    pub fn record_latency(&self, model_id: &str, latency_ms: f64) {
        let entry = self
            .latency_tracker
            .entry(model_id.to_string())
            .or_insert_with(|| LatencyEntry {
                avg_ms: AtomicU64::new(latency_ms.to_bits()),
                count: AtomicU64::new(0),
            });

        let count = entry.count.fetch_add(1, Ordering::Relaxed);
        if count == 0 {
            entry.avg_ms.store(latency_ms.to_bits(), Ordering::Relaxed);
        } else {
            // EMA: 90% old + 10% new (matching Python implementation)
            let old = f64::from_bits(entry.avg_ms.load(Ordering::Relaxed));
            let new_avg = old * 0.9 + latency_ms * 0.1;
            entry.avg_ms.store(new_avg.to_bits(), Ordering::Relaxed);
        }
    }

    /// Get the average latency for a model (in ms). Returns f64::MAX if unknown.
    pub fn get_avg_latency(&self, model_id: &str) -> f64 {
        self.latency_tracker
            .get(model_id)
            .map(|e| {
                let count = e.count.load(Ordering::Relaxed);
                if count == 0 {
                    f64::MAX
                } else {
                    f64::from_bits(e.avg_ms.load(Ordering::Relaxed))
                }
            })
            .unwrap_or(f64::MAX)
    }

    // =========================================================================
    // Active request tracking (for least-busy routing) — per-pod only
    // =========================================================================

    /// Increment active request count for a model. Returns the new count.
    pub fn begin_request(&self, model_id: &str) -> i64 {
        let counter = self
            .active_requests
            .entry(model_id.to_string())
            .or_insert_with(|| AtomicI64::new(0));
        counter.fetch_add(1, Ordering::Relaxed) + 1
    }

    /// Decrement active request count for a model.
    pub fn end_request(&self, model_id: &str) {
        if let Some(counter) = self.active_requests.get(model_id) {
            let prev = counter.fetch_sub(1, Ordering::Relaxed);
            if prev <= 0 {
                counter.store(0, Ordering::Relaxed);
            }
        }
    }

    /// Get active request count for a model.
    pub fn get_active_requests(&self, model_id: &str) -> i64 {
        self.active_requests
            .get(model_id)
            .map(|c| c.load(Ordering::Relaxed))
            .unwrap_or(0)
    }

    // =========================================================================
    // Group concurrency (max_parallel_requests)
    //
    // Enforcement moved OUT of RouterState. The old counter here was acquired at
    // routing-selection time but had no production release path (and its Redis
    // key carried no TTL), so it grew monotonically and permanently wedged a
    // routing group once `max_parallel_requests` lifetime requests had passed
    // through it. It is now an RAII slot in `usage::acquire_llm_concurrency`
    // (`LlmConcurrencyGuard`, key `llm:rg:{group_id}`) that releases on Drop
    // across success / error / stream-end / cancellation.
    // =========================================================================

    // =========================================================================
    // Tool call rate limiting (hourly / daily in-memory counters) — per-pod only
    // =========================================================================

    // =========================================================================
    // Cleanup (call periodically to evict stale entries)
    // =========================================================================

    /// Evict expired cooldowns and stale rate-limit counters.
    pub fn cleanup(&self) {
        let current_min = Self::current_minute_key();

        // Remove expired cooldowns
        self.cooldowns
            .retain(|_, v| v.started_at.elapsed() < v.duration);

        // Remove stale RPM/TPM counters (older than current minute)
        self.rpm_counters.retain(|k, _| k.ends_with(&current_min));
        self.tpm_counters.retain(|k, _| k.ends_with(&current_min));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    // =========================================================================
    // Cooldown tests
    // =========================================================================

    #[test]
    fn test_set_cooldown_and_check_active() {
        let state = RouterState::new(None);
        state.set_cooldown("model-a", Duration::from_secs(60), 429, "rate limited");
        assert!(state.is_cooled_down("model-a"));
    }

    #[test]
    fn test_cooldown_expired() {
        let state = RouterState::new(None);
        // Set a cooldown that expires immediately
        state.set_cooldown("model-a", Duration::from_millis(0), 429, "rate limited");
        // Should be expired right away
        assert!(!state.is_cooled_down("model-a"));
    }

    #[test]
    fn test_cooldown_not_set_returns_false() {
        let state = RouterState::new(None);
        assert!(!state.is_cooled_down("nonexistent-model"));
    }

    #[test]
    fn test_should_cooldown_for_rate_limit() {
        assert!(RouterState::should_cooldown(429));
    }

    #[test]
    fn test_should_cooldown_for_server_errors() {
        assert!(RouterState::should_cooldown(500));
        assert!(RouterState::should_cooldown(502));
        assert!(RouterState::should_cooldown(503));
        assert!(RouterState::should_cooldown(599));
    }

    #[test]
    fn test_should_cooldown_for_auth_errors() {
        assert!(RouterState::should_cooldown(401));
        assert!(RouterState::should_cooldown(403));
    }

    #[test]
    fn test_should_cooldown_for_timeout() {
        assert!(RouterState::should_cooldown(408));
    }

    #[test]
    fn test_should_not_cooldown_for_success() {
        assert!(!RouterState::should_cooldown(200));
    }

    #[test]
    fn test_should_not_cooldown_for_not_found() {
        assert!(!RouterState::should_cooldown(404));
    }

    #[test]
    fn test_record_failure_triggers_cooldown_for_429() {
        let state = RouterState::new(None);
        state.record_failure("model-a", 429, 30, "rate limited");
        assert!(state.is_cooled_down("model-a"));
    }

    #[test]
    fn test_record_failure_triggers_cooldown_for_500() {
        let state = RouterState::new(None);
        state.record_failure("model-a", 500, 30, "internal server error");
        assert!(state.is_cooled_down("model-a"));
    }

    #[test]
    fn test_record_failure_triggers_cooldown_for_401() {
        let state = RouterState::new(None);
        state.record_failure("model-a", 401, 30, "unauthorized");
        assert!(state.is_cooled_down("model-a"));
    }

    #[test]
    fn test_record_failure_triggers_cooldown_for_403() {
        let state = RouterState::new(None);
        state.record_failure("model-a", 403, 30, "forbidden");
        assert!(state.is_cooled_down("model-a"));
    }

    #[test]
    fn test_record_failure_triggers_cooldown_for_408() {
        let state = RouterState::new(None);
        state.record_failure("model-a", 408, 30, "timeout");
        assert!(state.is_cooled_down("model-a"));
    }

    #[test]
    fn test_record_failure_no_cooldown_for_200() {
        let state = RouterState::new(None);
        state.record_failure("model-a", 200, 30, "not an error");
        assert!(!state.is_cooled_down("model-a"));
    }

    #[test]
    fn test_record_failure_no_cooldown_for_404() {
        let state = RouterState::new(None);
        state.record_failure("model-a", 404, 30, "not found");
        assert!(!state.is_cooled_down("model-a"));
    }

    #[test]
    fn test_record_success_clears_cooldown() {
        let state = RouterState::new(None);
        state.set_cooldown("model-a", Duration::from_secs(60), 429, "rate limited");
        assert!(state.is_cooled_down("model-a"));
        state.record_success("model-a");
        assert!(!state.is_cooled_down("model-a"));
    }

    // =========================================================================
    // RPM tests
    // =========================================================================

    #[test]
    fn test_increment_rpm() {
        let state = RouterState::new(None);
        let count = state.increment_rpm("model-a");
        assert_eq!(count, 1);
        let count = state.increment_rpm("model-a");
        assert_eq!(count, 2);
    }

    #[test]
    fn test_get_rpm_unknown_model() {
        let state = RouterState::new(None);
        assert_eq!(state.get_rpm("unknown"), 0);
    }

    #[test]
    fn test_get_rpm_after_increments() {
        let state = RouterState::new(None);
        state.increment_rpm("model-a");
        state.increment_rpm("model-a");
        state.increment_rpm("model-a");
        assert_eq!(state.get_rpm("model-a"), 3);
    }

    #[test]
    fn test_check_rpm_below_limit() {
        let state = RouterState::new(None);
        state.increment_rpm("model-a");
        assert!(!state.check_rpm("model-a", 5));
    }

    #[test]
    fn test_check_rpm_at_limit() {
        let state = RouterState::new(None);
        for _ in 0..5 {
            state.increment_rpm("model-a");
        }
        assert!(state.check_rpm("model-a", 5));
    }

    #[test]
    fn test_check_rpm_above_limit() {
        let state = RouterState::new(None);
        for _ in 0..10 {
            state.increment_rpm("model-a");
        }
        assert!(state.check_rpm("model-a", 5));
    }

    // =========================================================================
    // TPM tests
    // =========================================================================

    #[test]
    fn test_add_tpm() {
        let state = RouterState::new(None);
        let count = state.add_tpm("model-a", 100);
        assert_eq!(count, 100);
        let count = state.add_tpm("model-a", 50);
        assert_eq!(count, 150);
    }

    #[test]
    fn test_get_tpm_unknown_model() {
        let state = RouterState::new(None);
        assert_eq!(state.get_tpm("unknown"), 0);
    }

    #[test]
    fn test_get_tpm_after_adds() {
        let state = RouterState::new(None);
        state.add_tpm("model-a", 200);
        state.add_tpm("model-a", 300);
        assert_eq!(state.get_tpm("model-a"), 500);
    }

    #[test]
    fn test_check_tpm_below_limit() {
        let state = RouterState::new(None);
        state.add_tpm("model-a", 500);
        assert!(!state.check_tpm("model-a", 1000));
    }

    #[test]
    fn test_check_tpm_at_limit() {
        let state = RouterState::new(None);
        state.add_tpm("model-a", 1000);
        assert!(state.check_tpm("model-a", 1000));
    }

    // =========================================================================
    // Latency tests
    // =========================================================================

    #[test]
    fn test_record_latency_first_sample() {
        let state = RouterState::new(None);
        state.record_latency("model-a", 100.0);
        let avg = state.get_avg_latency("model-a");
        assert!((avg - 100.0).abs() < 0.001);
    }

    #[test]
    fn test_record_latency_ema() {
        let state = RouterState::new(None);
        state.record_latency("model-a", 100.0);
        state.record_latency("model-a", 200.0);
        // EMA: 100.0 * 0.9 + 200.0 * 0.1 = 90.0 + 20.0 = 110.0
        let avg = state.get_avg_latency("model-a");
        assert!((avg - 110.0).abs() < 0.001, "Expected ~110.0, got {}", avg);
    }

    #[test]
    fn test_record_latency_multiple_ema() {
        let state = RouterState::new(None);
        state.record_latency("model-a", 100.0);
        state.record_latency("model-a", 200.0);
        // After 2nd: 100*0.9 + 200*0.1 = 110.0
        state.record_latency("model-a", 300.0);
        // After 3rd: 110*0.9 + 300*0.1 = 99.0 + 30.0 = 129.0
        let avg = state.get_avg_latency("model-a");
        assert!((avg - 129.0).abs() < 0.001, "Expected ~129.0, got {}", avg);
    }

    #[test]
    fn test_get_avg_latency_unknown_model() {
        let state = RouterState::new(None);
        assert_eq!(state.get_avg_latency("unknown"), f64::MAX);
    }

    // =========================================================================
    // Active request tests
    // =========================================================================

    #[test]
    fn test_begin_request_increments() {
        let state = RouterState::new(None);
        assert_eq!(state.begin_request("model-a"), 1);
        assert_eq!(state.begin_request("model-a"), 2);
        assert_eq!(state.get_active_requests("model-a"), 2);
    }

    #[test]
    fn test_end_request_decrements() {
        let state = RouterState::new(None);
        state.begin_request("model-a");
        state.begin_request("model-a");
        state.end_request("model-a");
        assert_eq!(state.get_active_requests("model-a"), 1);
    }

    #[test]
    fn test_end_request_never_below_zero() {
        let state = RouterState::new(None);
        state.begin_request("model-a");
        state.end_request("model-a");
        state.end_request("model-a"); // extra decrement
        assert!(state.get_active_requests("model-a") >= 0);
    }

    #[test]
    fn test_get_active_requests_unknown_model() {
        let state = RouterState::new(None);
        assert_eq!(state.get_active_requests("unknown"), 0);
    }

    // Group concurrency (`max_parallel_requests`) enforcement moved from
    // RouterState to `usage::LlmConcurrencyGuard` (RAII, released on Drop). Its
    // tests live alongside that guard in `services::usage`. The old
    // RouterState-counter tests were removed with the leaking counter.

    // =========================================================================
    // Cleanup tests
    // =========================================================================

    #[test]
    fn test_cleanup_removes_expired_cooldowns() {
        let state = RouterState::new(None);
        state.set_cooldown("model-a", Duration::from_millis(0), 429, "rate limited");
        // The cooldown is already expired (0ms duration)
        state.cleanup();
        // After cleanup, the cooldown entry should be gone
        assert!(!state.cooldowns.contains_key("model-a"));
    }

    #[test]
    fn test_cleanup_keeps_active_cooldowns() {
        let state = RouterState::new(None);
        state.set_cooldown("model-a", Duration::from_secs(60), 429, "rate limited");
        state.cleanup();
        assert!(state.cooldowns.contains_key("model-a"));
    }
}

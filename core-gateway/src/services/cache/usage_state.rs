// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Redis-Backed Usage State
//!
//! The shared counters every limit is enforced on: usage quotas, run and
//! session caps, rate buckets, in-flight slots — admitted atomically by the
//! [`admission`] engine, exact across every Core pod.
//!
//! The gateway does not run without Redis (main refuses to start). A Redis
//! that errors refuses the requests whose limits it holds (fail closed);
//! there is no per-pod fallback, which would silently multiply every limit by
//! the replica count. `UsageState::new(None)` is the in-process store the
//! tests run on (`mem_kv`), under the same keys.

use chrono::{DateTime, Datelike, Utc};
use dashmap::DashMap;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

mod admission;
mod mem_kv;

pub use admission::{
    Actual, Admission, Dim, DimKind, Fallback, Finish, HOLD_LEASE_MS, HOLD_RENEW_SECS, HoldGuard,
    Measure, Redirect, Shape, Verdict,
};

/// Fixed-window quota granularity. `Hour`/`Day` are rolling windows aligned to
/// the Unix epoch (so `Day` boundaries fall on UTC midnight); `Month` is a true
/// calendar month (variable length), indexed by `year*12 + month`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum QuotaWindow {
    Hour,
    Day,
    Month,
}

impl QuotaWindow {
    /// Nominal window length in seconds. For `Month` this is an approximation
    /// (30 days) used only where a coarse duration is acceptable — keying and
    /// TTL use [`QuotaWindow::index`] / [`QuotaWindow::ttl_secs`] instead, which
    /// are calendar-correct.
    pub fn secs(self) -> u64 {
        match self {
            QuotaWindow::Hour => 3600,
            QuotaWindow::Day => 86400,
            QuotaWindow::Month => 2_592_000,
        }
    }

    /// Short label used in Redis keys.
    pub fn label(self) -> &'static str {
        match self {
            QuotaWindow::Hour => "h",
            QuotaWindow::Day => "d",
            QuotaWindow::Month => "m",
        }
    }

    /// Stable index for the window containing `now`. Embedded in the counter
    /// key so the counter auto-rolls at the window boundary. `Hour`/`Day` divide
    /// the epoch timestamp (matching the legacy `now_secs / secs()` scheme);
    /// `Month` is `year*12 + month0` so December→January rolls correctly.
    pub fn index(self, now: DateTime<Utc>) -> i64 {
        match self {
            QuotaWindow::Hour => now.timestamp() / 3600,
            QuotaWindow::Day => now.timestamp() / 86400,
            QuotaWindow::Month => now.year() as i64 * 12 + now.month0() as i64,
        }
    }

    /// TTL for a counter key: window length + generous grace, so the previous
    /// window's key self-evicts well after it stops being read.
    pub fn ttl_secs(self) -> u64 {
        match self {
            QuotaWindow::Hour => 2 * 3600,
            QuotaWindow::Day => 2 * 86400,
            QuotaWindow::Month => 35 * 86400,
        }
    }
}

/// Raise a usage counter to at least the ledger value, atomically; never
/// lower it.
///
/// ARGV: value, ttl. Returns 1 when it raised the counter. Being a single
/// script it cannot interleave with an in-flight `INCRBY`/reserve, and since
/// it only ever increases, a concurrent increment can be over-counted
/// briefly, never lost. A fresh key gets the window TTL; an existing key
/// keeps its own.
const USAGE_RAISE_TO_LUA: &str = r#"
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
local v = tonumber(ARGV[1])
if v <= cur then
  return 0
end
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('SET', KEYS[1], v, 'KEEPTTL')
else
  redis.call('SET', KEYS[1], v, 'EX', tonumber(ARGV[2]))
end
return 1
"#;

/// Atomic counter increment: adjust a counter by a (possibly negative) delta,
/// flooring at zero so it can never go negative, and (re)set the window TTL.
/// Used by [`shadow_settle`] to accumulate each request's actual usage into the
/// quota counters. ARGV: delta, ttl.
const SETTLE_LUA: &str = r#"
local newv = redis.call('INCRBY', KEYS[1], tonumber(ARGV[1]))
if newv < 0 then
  redis.call('SET', KEYS[1], '0')
  newv = 0
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return newv
"#;

/// How long a run's counters outlive their last hop. Runs are finalized by
/// the idle sweeper long before this; the TTL only bounds abandoned keys.
///
/// A run parked on a human approval is the exception: its keys are kept alive
/// for the hold window on top of this (see [`UsageState::run_keep_alive`]),
/// because a `t0` that expired mid-hold would be re-created on the retry and
/// hand the run a fresh clock — a run could escape its ceiling by waiting.
pub const RUN_TTL_SECS: u64 = 86_400;

/// Every per-run key. Kept in one list so a keep-alive cannot extend some of
/// a run's counters and let the others lapse.
const RUN_FIELDS: [&str; 6] = ["t0", "paused", "calls", "tokens", "cost", "actions"];

pub struct UsageState {
    redis: Option<redis::aio::ConnectionManager>,
    /// Run clocks for `new(None)` (tests): start and paused time. The run's
    /// counted terms live in `mem` under their Redis keys.
    mem_run: DashMap<String, MemRun>,
    /// The keyspace stand-in for `new(None)` (tests): the admission engine
    /// and the usage counters, under the same keys Redis would hold.
    mem: mem_kv::MemKv,
    /// Admission holds this replica owns: renewed while it lives, ended by
    /// settle or by their guard (see `admission`).
    live_holds: DashMap<String, std::sync::Arc<admission::LiveHold>>,
}

/// One run's accumulated spend, as the gate sees it.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct RunUsage {
    pub calls: i64,
    pub tokens: i64,
    pub cost_usd: f64,
    /// Seconds the run has been EXECUTING: since its first governed call,
    /// less the time it spent parked on a human approval (`paused_ms`).
    /// The per-run duration ceiling bounds the agent's own runtime; a wait
    /// for a person is governance, not runtime.
    pub age_secs: i64,
    /// Wall-clock start of the run's clock (epoch ms); 0 when unknown.
    pub started_ms: i64,
    /// Hold time already subtracted from `age_secs`.
    pub paused_ms: i64,
}

impl RunUsage {
    /// The same usage with a (larger) paused figure applied. Paused time only
    /// ever grows — see [`UsageState::run_record_paused`].
    pub fn with_paused(self, paused_ms: i64, now_ms: i64) -> Self {
        let paused_ms = paused_ms.max(self.paused_ms);
        if self.started_ms <= 0 {
            return Self { paused_ms, ..self };
        }
        Self {
            age_secs: ((now_ms - self.started_ms - paused_ms).max(0)) / 1000,
            paused_ms,
            ..self
        }
    }
}

pub(crate) fn now_epoch_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

#[derive(Debug, Clone, Copy)]
struct MemRun {
    started: Instant,
    paused_ms: i64,
}

impl MemRun {
    fn new() -> Self {
        Self {
            started: Instant::now(),
            paused_ms: 0,
        }
    }
}

impl UsageState {
    pub fn new(redis: Option<redis::aio::ConnectionManager>) -> Self {
        Self {
            redis,
            mem_run: DashMap::new(),
            mem: mem_kv::MemKv::default(),
            live_holds: DashMap::new(),
        }
    }

    pub fn has_redis(&self) -> bool {
        self.redis.is_some()
    }

    fn current_minute_key() -> String {
        let secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        format!("{}", secs / 60)
    }

    fn current_hour_key() -> String {
        let secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        format!("{}", secs / 3600)
    }

    fn current_day_key() -> String {
        let secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        format!("{}", secs / 86400)
    }

    // -----------------------------------------------------------------------
    // RPM (requests per minute)
    // -----------------------------------------------------------------------

    /// Increment RPM counter and return the new value.
    /// Uses Redis INCR with auto-expire for cross-pod aggregation.
    pub async fn increment_rpm(&self, model_id: &str) -> i64 {
        let key = format!("brutor:rpm:{}:{}", model_id, Self::current_minute_key());
        self.incr_with_ttl(&key, 1, 120).await
    }

    /// Get current RPM for a model from Redis.
    pub async fn get_rpm(&self, model_id: &str) -> i64 {
        let key = format!("brutor:rpm:{}:{}", model_id, Self::current_minute_key());
        self.get_i64(&key).await
    }

    /// Check if RPM exceeds limit.
    pub async fn check_rpm(&self, model_id: &str, limit: i32) -> bool {
        self.get_rpm(model_id).await >= limit as i64
    }

    // -----------------------------------------------------------------------
    // TPM (tokens per minute)
    // -----------------------------------------------------------------------

    /// Increment TPM counter by `tokens` and return the new value.
    pub async fn add_tpm(&self, model_id: &str, tokens: i64) -> i64 {
        let key = format!("brutor:tpm:{}:{}", model_id, Self::current_minute_key());
        self.incr_with_ttl(&key, tokens, 120).await
    }

    /// Get current TPM for a model from Redis.
    pub async fn get_tpm(&self, model_id: &str) -> i64 {
        let key = format!("brutor:tpm:{}:{}", model_id, Self::current_minute_key());
        self.get_i64(&key).await
    }

    /// Check if TPM exceeds limit.
    pub async fn check_tpm(&self, model_id: &str, limit: i32) -> bool {
        self.get_tpm(model_id).await >= limit as i64
    }

    // -----------------------------------------------------------------------
    // Cooldowns
    // -----------------------------------------------------------------------

    /// Set a cooldown for a model. TTL = cooldown duration.
    pub async fn set_cooldown(&self, model_id: &str, ttl_secs: u64) {
        let key = format!("brutor:cooldown:{model_id}");
        self.set_with_ttl(&key, "1", ttl_secs).await;
    }

    /// Check if a model is in cooldown.
    pub async fn is_cooled_down(&self, model_id: &str) -> bool {
        let key = format!("brutor:cooldown:{model_id}");
        self.exists(&key).await
    }

    /// Clear a cooldown for a model.
    pub async fn clear_cooldown(&self, model_id: &str) {
        let key = format!("brutor:cooldown:{model_id}");
        self.del(&key).await;
    }

    // -----------------------------------------------------------------------
    // Active requests (for least-busy routing)
    // -----------------------------------------------------------------------

    /// Increment active request count. Returns new value.
    pub async fn begin_request(&self, model_id: &str) -> i64 {
        let key = format!("brutor:active:{model_id}");
        self.incr_with_ttl(&key, 1, 300).await // 5min safety TTL
    }

    /// Decrement active request count (floored at 0).
    pub async fn end_request(&self, model_id: &str) {
        let key = format!("brutor:active:{model_id}");
        self.decr_floor_zero(&key).await;
    }

    /// Get active request count.
    pub async fn get_active_requests(&self, model_id: &str) -> i64 {
        let key = format!("brutor:active:{model_id}");
        self.get_i64(&key).await
    }

    // -----------------------------------------------------------------------
    // Per-run counters (run-level caps)
    //
    // Scoped to one `root_task_id` — the run id minted at request entry — so
    // a runaway agent loop is stopped at ITS OWN expense rather than after
    // it has consumed the whole resource group's daily budget and 429'd
    // every other system in the group until midnight.
    //
    // Cost is accumulated in integer micro-dollars: Redis INCRBY is integer,
    // and float accumulation over thousands of hops drifts.
    // -----------------------------------------------------------------------

    fn run_key(tenant_id: &str, run_id: &str, field: &str) -> String {
        format!("brutor:run:{}:{}:{}", tenant_id, run_id, field)
    }

    /// The key of one of a run's counted terms (`calls`, `tokens`, `cost`):
    /// what run-cap admission dimensions reserve and settle against.
    pub fn run_counter_key(tenant_id: &str, run_id: &str, field: &str) -> String {
        Self::run_key(tenant_id, run_id, field)
    }

    /// Read the run's usage so far, establishing its start clock on first
    /// touch. Called by the gate BEFORE the upstream request.
    ///
    /// `Err` when the shared counters cannot be read. It used to read
    /// as an empty run (every counter 0), which let every call through while
    /// Redis was erroring; the caller now fails closed.
    pub async fn run_usage(
        &self,
        tenant_id: &str,
        run_id: &str,
    ) -> Result<RunUsage, redis::RedisError> {
        let now_ms = now_epoch_ms();

        if self.redis.is_some() {
            let t0_key = Self::run_key(tenant_id, run_id, "t0");
            // First writer wins: the run's clock starts at its first
            // governed call, not at whichever pod happens to serve hop N.
            self.set_nx_with_ttl(&t0_key, &now_ms.to_string(), RUN_TTL_SECS)
                .await;
            let t0 = self.try_get_i64(&t0_key).await?;
            let paused = self
                .try_get_i64(&Self::run_key(tenant_id, run_id, "paused"))
                .await?
                .max(0);
            let calls = self
                .try_get_i64(&Self::run_key(tenant_id, run_id, "calls"))
                .await?;
            let tokens = self
                .try_get_i64(&Self::run_key(tenant_id, run_id, "tokens"))
                .await?;
            let micros = self
                .try_get_i64(&Self::run_key(tenant_id, run_id, "cost"))
                .await?;
            return Ok(RunUsage {
                calls,
                tokens,
                cost_usd: micros as f64 / 1_000_000.0,
                age_secs: if t0 > 0 {
                    (now_ms - t0 - paused).max(0) / 1000
                } else {
                    0
                },
                started_ms: t0.max(0),
                paused_ms: paused,
            });
        }

        let key = format!("{}:{}", tenant_id, run_id);
        let entry = self.mem_run.entry(key).or_insert_with(MemRun::new);
        let elapsed_ms = entry.started.elapsed().as_millis() as i64;
        let paused_ms = entry.paused_ms;
        drop(entry);
        let mut tx = self.mem.lock(now_ms);
        let calls = tx.get_int(&Self::run_key(tenant_id, run_id, "calls"));
        let tokens = tx.get_int(&Self::run_key(tenant_id, run_id, "tokens"));
        let micros = tx.get_int(&Self::run_key(tenant_id, run_id, "cost"));
        Ok(RunUsage {
            calls,
            tokens,
            cost_usd: micros as f64 / 1_000_000.0,
            age_secs: (elapsed_ms - paused_ms).max(0) / 1000,
            started_ms: now_ms - elapsed_ms,
            paused_ms,
        })
    }

    /// Wall-clock start of a run's clock (epoch ms), WITHOUT starting it.
    /// `None` when no governed call has touched the run yet — there is then
    /// no clock to pause.
    pub async fn run_started_ms(&self, tenant_id: &str, run_id: &str) -> Option<i64> {
        if self.redis.is_some() {
            return self
                .get_i64_opt(&Self::run_key(tenant_id, run_id, "t0"))
                .await
                .filter(|t0| *t0 > 0);
        }
        self.mem_run
            .get(&format!("{}:{}", tenant_id, run_id))
            .map(|e| now_epoch_ms() - e.started.elapsed().as_millis() as i64)
    }

    /// Move a run's clock start `by_ms` into the past — as if the run had
    /// been open that much longer. Test support only (the `db-tests` suite):
    /// it lets a duration-ceiling test put the run past its ceiling by
    /// construction instead of racing the wall clock with `sleep`, which is
    /// what made those tests flaky under load. No-op for a run with no clock.
    #[cfg(any(test, feature = "db-tests"))]
    pub async fn run_backdate_start(&self, tenant_id: &str, run_id: &str, by_ms: i64) {
        if self.redis.is_some() {
            let key = Self::run_key(tenant_id, run_id, "t0");
            if let Some(t0) = self.get_i64_opt(&key).await.filter(|t| *t > 0) {
                if let Some(ref redis) = self.redis {
                    let mut conn = redis.clone();
                    let _: Result<(), _> = redis::cmd("SET")
                        .arg(&key)
                        .arg(t0 - by_ms)
                        .arg("KEEPTTL")
                        .query_async(&mut conn)
                        .await;
                }
            }
            return;
        }
        if let Some(mut entry) = self.mem_run.get_mut(&format!("{}:{}", tenant_id, run_id)) {
            if let Some(earlier) = entry
                .started
                .checked_sub(std::time::Duration::from_millis(by_ms.max(0) as u64))
            {
                entry.started = earlier;
            }
        }
    }

    /// Record how long the run has spent parked on a human approval, and keep
    /// its keys alive for at least `keep_alive_secs`.
    ///
    /// Monotone: the stored figure is the MAX of what is there and
    /// `paused_ms`. Callers compute it from `tool_approval_requests` (merged
    /// intervals, the ledger's own rule) rather than adding one hold at a time,
    /// so two pods reconciling the same run concurrently converge on the same
    /// value instead of double-counting it.
    pub async fn run_record_paused(
        &self,
        tenant_id: &str,
        run_id: &str,
        paused_ms: i64,
        keep_alive_secs: u64,
    ) {
        let paused_ms = paused_ms.max(0);
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let script = redis::Script::new(
                r#"
                local cur = tonumber(redis.call('GET', KEYS[1]) or '0') or 0
                local new = tonumber(ARGV[1])
                if new > cur then
                    redis.call('SET', KEYS[1], new)
                end
                return 0
                "#,
            );
            let key = Self::run_key(tenant_id, run_id, "paused");
            let result: Result<i64, _> = script
                .key(&key)
                .arg(paused_ms)
                .invoke_async(&mut conn)
                .await;
            if let Err(e) = result {
                tracing::debug!(key, error = %e, "Redis run pause update failed");
            }
            self.run_keep_alive(tenant_id, run_id, keep_alive_secs.max(RUN_TTL_SECS))
                .await;
            return;
        }
        if let Some(mut entry) = self.mem_run.get_mut(&format!("{}:{}", tenant_id, run_id)) {
            entry.paused_ms = entry.paused_ms.max(paused_ms);
        }
    }

    /// Extend every one of the run's keys to live at least `secs` more.
    ///
    /// Called when a hold is raised (for the hold window plus the normal run
    /// TTL) and when one is redeemed. Only ever lengthens a TTL, and only on
    /// keys that exist — a keep-alive must not create a `t0` and start a
    /// clock the run never started. Redis-only: the per-pod fallback holds
    /// no TTL, it is dropped when the run finalizes.
    pub async fn run_keep_alive(&self, tenant_id: &str, run_id: &str, secs: u64) {
        let Some(ref redis) = self.redis else {
            return;
        };
        let mut conn = redis.clone();
        let script = redis::Script::new(
            r#"
            local ttl = tonumber(ARGV[1])
            for i, k in ipairs(KEYS) do
                -- -2: no such key (never create one); -1: no expiry yet
                -- (the pause key right after its first SET).
                local cur = redis.call('TTL', k)
                if cur == -1 or (cur >= 0 and cur < ttl) then
                    redis.call('EXPIRE', k, ttl)
                end
            end
            return 0
            "#,
        );
        let mut inv = script.prepare_invoke();
        for field in RUN_FIELDS {
            inv.key(Self::run_key(tenant_id, run_id, field));
        }
        inv.arg(secs);
        let result: Result<i64, _> = inv.invoke_async(&mut conn).await;
        if let Err(e) = result {
            tracing::debug!(run_id, error = %e, "Redis run keep-alive failed");
        }
    }

    /// Record one completed hop against the run with its actual figures.
    /// Test support and paths without an admission: a governed LLM call books
    /// its run usage by settling its admission hold.
    pub async fn run_commit(&self, tenant_id: &str, run_id: &str, tokens: i64, cost_usd: f64) {
        let micros = (cost_usd * 1_000_000.0).round() as i64;
        if self.redis.is_some() {
            self.incr_with_ttl(&Self::run_key(tenant_id, run_id, "calls"), 1, RUN_TTL_SECS)
                .await;
            if tokens > 0 {
                self.incr_with_ttl(
                    &Self::run_key(tenant_id, run_id, "tokens"),
                    tokens,
                    RUN_TTL_SECS,
                )
                .await;
            }
            if micros > 0 {
                self.incr_with_ttl(
                    &Self::run_key(tenant_id, run_id, "cost"),
                    micros,
                    RUN_TTL_SECS,
                )
                .await;
            }
            return;
        }
        self.mem_run
            .entry(format!("{}:{}", tenant_id, run_id))
            .or_insert_with(MemRun::new);
        let mut tx = self.mem.lock(now_epoch_ms());
        let ttl = Some(RUN_TTL_SECS as i64 * 1000);
        tx.incr_floor(&Self::run_key(tenant_id, run_id, "calls"), 1, ttl, false);
        tx.incr_floor(
            &Self::run_key(tenant_id, run_id, "tokens"),
            tokens.max(0),
            ttl,
            false,
        );
        tx.incr_floor(
            &Self::run_key(tenant_id, run_id, "cost"),
            micros.max(0),
            ttl,
            false,
        );
    }

    // -----------------------------------------------------------------------
    // Raw counters (the conversation ceiling, see `session_budget`)
    // -----------------------------------------------------------------------

    /// Set `key` to `value` with `ttl_secs` unless it already exists: seeds a
    /// live counter from the ledger without ever clobbering one in use.
    pub async fn seed_key_if_absent(
        &self,
        key: &str,
        value: i64,
        ttl_secs: u64,
    ) -> Result<bool, redis::RedisError> {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let r: Option<String> = redis::cmd("SET")
                .arg(key)
                .arg(value)
                .arg("NX")
                .arg("EX")
                .arg(ttl_secs)
                .query_async(&mut conn)
                .await?;
            return Ok(r.is_some());
        }
        let mut tx = self.mem.lock(now_epoch_ms());
        if tx.exists(key) {
            return Ok(false);
        }
        tx.set_int(key, value, Some(ttl_secs as i64 * 1000));
        Ok(true)
    }

    // -----------------------------------------------------------------------
    // Tool calls emitted per group (LLM governance `tool_use` limits)
    // -----------------------------------------------------------------------

    fn tool_call_keys(group_id: &str) -> [String; 2] {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        [
            format!("brutor:toolc:{group_id}:h:{}", now / 3600),
            format!("brutor:toolc:{group_id}:d:{}", now / 86_400),
        ]
    }

    /// Add `n` emitted tool calls to the group's hour and day counters,
    /// atomically per key on the shared store, returning the new totals —
    /// so two replicas' responses crossing the limit together cannot both
    /// read "under". `Err` when the store cannot be reached (fail closed).
    pub async fn tool_calls_add(
        &self,
        group_id: &str,
        n: i64,
    ) -> Result<(i64, i64), redis::RedisError> {
        let [h, d] = Self::tool_call_keys(group_id);
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let (hv, dv): (i64, i64) = redis::pipe()
                .atomic()
                .incr(&h, n)
                .expire(&h, 7_200)
                .ignore()
                .incr(&d, n)
                .expire(&d, 172_800)
                .ignore()
                .query_async(&mut conn)
                .await?;
            return Ok((hv, dv));
        }
        let mut tx = self.mem.lock(now_epoch_ms());
        Ok((
            tx.incr_floor(&h, n, Some(7_200_000), false),
            tx.incr_floor(&d, n, Some(172_800_000), false),
        ))
    }

    /// The group's hour and day tool-call totals so far.
    pub async fn tool_calls_get(&self, group_id: &str) -> Result<(i64, i64), redis::RedisError> {
        let [h, d] = Self::tool_call_keys(group_id);
        Ok((self.counter_value(&h).await?, self.counter_value(&d).await?))
    }

    /// Delete a key (best effort).
    pub async fn delete_key(&self, key: &str) {
        if self.redis.is_some() {
            self.del(key).await;
        } else {
            self.mem.lock(now_epoch_ms()).del(key);
        }
    }

    /// Drop a finalized run's counters. Best-effort tidy-up; the TTL is the
    /// real guarantee.
    pub fn forget_run(&self, tenant_id: &str, run_id: &str) {
        self.mem_run.remove(&format!("{}:{}", tenant_id, run_id));
        if self.redis.is_none() {
            let mut tx = self.mem.lock(now_epoch_ms());
            for f in ["calls", "tokens", "cost", "actions"] {
                tx.del(&Self::run_key(tenant_id, run_id, f));
            }
        }
    }

    async fn set_nx_with_ttl(&self, key: &str, value: &str, ttl_secs: u64) {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let _: Result<i64, _> = redis::cmd("SET")
                .arg(key)
                .arg(value)
                .arg("NX")
                .arg("EX")
                .arg(ttl_secs)
                .query_async(&mut conn)
                .await;
        }
    }

    // -----------------------------------------------------------------------
    // Tool call counters
    // -----------------------------------------------------------------------

    /// Increment hourly tool call counter.
    pub async fn increment_tool_calls_hour(&self, group_id: &str, count: i64) -> i64 {
        let key = format!("brutor:tool_hour:{}:{}", group_id, Self::current_hour_key());
        self.incr_with_ttl(&key, count, 7200).await // 2h TTL
    }

    /// Increment daily tool call counter.
    pub async fn increment_tool_calls_day(&self, group_id: &str, count: i64) -> i64 {
        let key = format!("brutor:tool_day:{}:{}", group_id, Self::current_day_key());
        self.incr_with_ttl(&key, count, 172800).await // 48h TTL
    }

    /// Get hourly tool call count.
    pub async fn get_tool_calls_hour(&self, group_id: &str) -> i64 {
        let key = format!("brutor:tool_hour:{}:{}", group_id, Self::current_hour_key());
        self.get_i64(&key).await
    }

    /// Get daily tool call count.
    pub async fn get_tool_calls_day(&self, group_id: &str) -> i64 {
        let key = format!("brutor:tool_day:{}:{}", group_id, Self::current_day_key());
        self.get_i64(&key).await
    }

    // -----------------------------------------------------------------------
    // Cumulative usage counters — reserve/commit substrate for the quota
    // refactor. Redis is the live oracle; `usage_logs` remains the durable
    // ledger. All amounts are i64 (cost carried as micro-USD by callers) so a
    // single integer API covers tokens / requests / cost without float drift.
    // Redis-down → per-pod in-memory fallback (best-effort; the reconcile job
    // heals drift from the ledger).
    // -----------------------------------------------------------------------

    /// The full key of a cumulative counter in `window`'s CURRENT index.
    /// Admission dimensions use exactly this key, so whatever reads a counter
    /// sees what the admission reserved.
    pub fn usage_key(counter_key: &str, window: QuotaWindow) -> String {
        Self::usage_redis_key(counter_key, window, window.index(Utc::now()))
    }

    fn usage_redis_key(counter_key: &str, window: QuotaWindow, idx: i64) -> String {
        format!("brutor:uq:{}:{}:{}", window.label(), counter_key, idx)
    }

    /// Like [`read_usage`](Self::read_usage) but returns `None` when the Redis
    /// read FAILS (vs `Some(0)` for "no usage yet"). The snapshot path uses
    /// this to fall back to the authoritative ledger on a Redis outage
    /// instead of reading 0. Enforcement does not read-then-decide at all: it
    /// admits ([`Self::admit`]).
    pub async fn try_read_usage(&self, counter_key: &str, window: QuotaWindow) -> Option<i64> {
        let key = Self::usage_key(counter_key, window);
        match self.redis.as_ref() {
            Some(redis) => {
                let mut conn = redis.clone();
                let result: Result<Option<i64>, _> =
                    redis::cmd("GET").arg(&key).query_async(&mut conn).await;
                match result {
                    Ok(v) => Some(v.unwrap_or(0)),
                    Err(e) => {
                        tracing::warn!(key = %key, error = %e, "try_read_usage: Redis read failed — caller will fall back to ledger");
                        None
                    }
                }
            }
            None => Some(self.mem.lock(now_epoch_ms()).get_int(&key)),
        }
    }

    /// Apply a (possibly negative) delta to a cumulative counter in the
    /// current window, flooring at zero.
    pub async fn settle_usage(&self, counter_key: &str, window: QuotaWindow, delta: i64) {
        if delta == 0 {
            return;
        }
        if let Some(ref redis) = self.redis {
            shadow_settle(redis.clone(), counter_key.to_string(), window, delta).await;
            return;
        }
        let key = Self::usage_key(counter_key, window);
        self.mem.lock(now_epoch_ms()).incr_floor(
            &key,
            delta,
            Some(window.ttl_secs() as i64 * 1000),
            false,
        );
    }

    /// Current counter value for the active window (0 if unset, and 0 on a
    /// Redis error — a display read; nothing enforces on it).
    pub async fn read_usage(&self, counter_key: &str, window: QuotaWindow) -> i64 {
        self.try_read_usage(counter_key, window).await.unwrap_or(0)
    }

    /// Force-set a counter to `value` (overwriting), with the window TTL. Used
    /// by the one-time shadow baseline backfill when shadow is first enabled on
    /// a system that already has ledger usage this window — `seed_usage_if_absent`
    /// can't fix that case because the counter is already non-zero.
    pub async fn set_usage(&self, counter_key: &str, window: QuotaWindow, value: i64) {
        let key = Self::usage_key(counter_key, window);
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let r: Result<(), _> = redis::cmd("SET")
                .arg(&key)
                .arg(value.max(0))
                .arg("EX")
                .arg(window.ttl_secs() as i64)
                .query_async(&mut conn)
                .await;
            if let Err(e) = r {
                tracing::warn!(key = %key, error = %e, "set_usage failed");
            }
            return;
        }
        self.mem.lock(now_epoch_ms()).set_int(
            &key,
            value.max(0),
            Some(window.ttl_secs() as i64 * 1000),
        );
    }

    /// Claim the one-time shadow baseline (SET NX a sentinel). Returns `true`
    /// exactly once per Redis lifetime, so the startup backfill runs once when
    /// shadow is first enabled (and again only if Redis — and thus the counters
    /// — were flushed). No-op-true without Redis (tests / mem mode).
    pub async fn try_claim_shadow_baseline(&self) -> bool {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let r: Result<Option<String>, _> = redis::cmd("SET")
                .arg("brutor:uq:baseline:done")
                .arg("1")
                .arg("NX")
                .query_async(&mut conn)
                .await;
            return matches!(r, Ok(Some(_)));
        }
        true
    }

    /// Seed a counter from the durable ledger on a cold window — but only if it
    /// isn't already populated (so live reserves are never clobbered). Returns
    /// `true` if it actually seeded. Backfill for the reserve/commit cutover.
    pub async fn seed_usage_if_absent(
        &self,
        counter_key: &str,
        window: QuotaWindow,
        value: i64,
    ) -> bool {
        if value < 0 {
            return false;
        }
        let key = Self::usage_key(counter_key, window);
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            // SET … NX EX: set only if absent, with the window TTL.
            let result: Result<Option<String>, _> = redis::cmd("SET")
                .arg(&key)
                .arg(value)
                .arg("NX")
                .arg("EX")
                .arg(window.ttl_secs() as i64)
                .query_async(&mut conn)
                .await;
            return match result {
                Ok(opt) => opt.is_some(),
                Err(e) => {
                    tracing::warn!(key = %key, error = %e, "Redis seed_usage_if_absent failed");
                    false
                }
            };
        }
        let mut tx = self.mem.lock(now_epoch_ms());
        if tx.exists(&key) {
            return false;
        }
        tx.set_int(&key, value, Some(window.ttl_secs() as i64 * 1000));
        true
    }

    /// Raise `counter_key`'s current-window value to at least `value`; never
    /// lower it. The reconcile heal: a counter that missed increments
    /// while Redis was erroring is brought back up to the ledger, which a
    /// cold-only seed could not do. Returns true when it raised the counter.
    pub async fn raise_usage_to(&self, counter_key: &str, window: QuotaWindow, value: i64) -> bool {
        if value <= 0 {
            return false;
        }
        let key = Self::usage_key(counter_key, window);
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let result: Result<i64, _> = redis::Script::new(USAGE_RAISE_TO_LUA)
                .key(&key)
                .arg(value)
                .arg(window.ttl_secs() as i64)
                .invoke_async(&mut conn)
                .await;
            return match result {
                Ok(raised) => raised == 1,
                Err(e) => {
                    tracing::warn!(key = %key, error = %e, "Redis raise_usage_to failed");
                    false
                }
            };
        }
        let mut tx = self.mem.lock(now_epoch_ms());
        if value > tx.get_int(&key) {
            tx.set_int_keepttl(&key, value, Some(window.ttl_secs() as i64 * 1000));
            true
        } else {
            false
        }
    }

    // -----------------------------------------------------------------------
    // Low-level Redis operations
    // -----------------------------------------------------------------------

    async fn incr_with_ttl(&self, key: &str, amount: i64, ttl_secs: u64) -> i64 {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let result: Result<i64, _> = redis::pipe()
                .atomic()
                .incr(key, amount)
                .expire(key, ttl_secs as i64)
                .ignore()
                .query_async(&mut conn)
                .await;
            match result {
                Ok(val) => val,
                Err(e) => {
                    tracing::debug!(key, error = %e, "Redis INCR failed");
                    0
                }
            }
        } else {
            0
        }
    }

    async fn get_i64(&self, key: &str) -> i64 {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let result: Result<i64, _> = redis::cmd("GET").arg(key).query_async(&mut conn).await;
            result.unwrap_or(0)
        } else {
            0
        }
    }

    /// `None` on a cache MISS, as distinct from a cached zero.
    ///
    /// `get_i64` collapses both to 0, which is right for a counter that
    /// starts at zero but wrong for a read-through cache: a miss must send
    /// the caller to the ledger, while a cached zero must not.
    /// A counter that must be READ to enforce: absent is 0, an error is an
    /// error. `get_i64` folds both into 0, which is right for best-effort
    /// reads and wrong for a ceiling.
    async fn try_get_i64(&self, key: &str) -> Result<i64, redis::RedisError> {
        let Some(redis) = self.redis.as_ref() else {
            return Ok(0);
        };
        let mut conn = redis.clone();
        let v: Option<i64> = redis::cmd("GET").arg(key).query_async(&mut conn).await?;
        Ok(v.unwrap_or(0))
    }

    async fn get_i64_opt(&self, key: &str) -> Option<i64> {
        let redis = self.redis.as_ref()?;
        let mut conn = redis.clone();
        let result: Result<Option<i64>, _> =
            redis::cmd("GET").arg(key).query_async(&mut conn).await;
        result.ok().flatten()
    }

    async fn set_with_ttl(&self, key: &str, value: &str, ttl_secs: u64) {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let result: Result<(), _> = redis::cmd("SET")
                .arg(key)
                .arg(value)
                .arg("EX")
                .arg(ttl_secs as i64)
                .query_async(&mut conn)
                .await;
            if let Err(e) = result {
                tracing::debug!(key, error = %e, "Redis SET failed");
            }
        }
    }

    async fn exists(&self, key: &str) -> bool {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let result: Result<bool, _> =
                redis::cmd("EXISTS").arg(key).query_async(&mut conn).await;
            result.unwrap_or(false)
        } else {
            false
        }
    }

    async fn del(&self, key: &str) {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let _: Result<(), _> = redis::cmd("DEL").arg(key).query_async(&mut conn).await;
        }
    }

    async fn decr_floor_zero(&self, key: &str) {
        if let Some(ref redis) = self.redis {
            let mut conn = redis.clone();
            let script = redis::Script::new(
                r#"
                local val = redis.call('DECR', KEYS[1])
                if val < 0 then
                    redis.call('SET', KEYS[1], 0)
                    return 0
                end
                return val
                "#,
            );
            let _: Result<i64, _> = script.key(key).invoke_async(&mut conn).await;
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

/// Direct Redis counter increment for callers that hold only a cloned
/// connection (the background writer's fire-and-forget usage increment) rather
/// than the full `UsageState`. The key scheme is identical to the rest of this
/// module so [`UsageState::read_usage`] sees the same counter. Best-effort — a
/// failed increment only costs counter accuracy (the cold-start seed heals it),
/// never enforcement.
pub async fn shadow_settle(
    mut conn: redis::aio::ConnectionManager,
    counter_key: String,
    window: QuotaWindow,
    delta: i64,
) {
    if delta == 0 {
        return;
    }
    let idx = window.index(Utc::now());
    let key = UsageState::usage_redis_key(&counter_key, window, idx);
    let script = redis::Script::new(SETTLE_LUA);
    let result: Result<i64, _> = script
        .key(&key)
        .arg(delta)
        .arg(window.ttl_secs() as i64)
        .invoke_async(&mut conn)
        .await;
    if let Err(e) = result {
        tracing::warn!(key = %key, error = %e, "shadow_settle failed (reconcile will heal)");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    // -----------------------------------------------------------------------
    // Reserve/commit usage-counter substrate (in-memory fallback path).
    // -----------------------------------------------------------------------

    #[test]
    fn window_index_day_matches_epoch_division() {
        let now = Utc.timestamp_opt(1_700_000_000, 0).unwrap();
        assert_eq!(QuotaWindow::Day.index(now), 1_700_000_000 / 86400);
        assert_eq!(QuotaWindow::Hour.index(now), 1_700_000_000 / 3600);
    }

    #[test]
    fn window_index_month_is_calendar_and_rolls() {
        let dec = Utc.with_ymd_and_hms(2025, 12, 15, 0, 0, 0).unwrap();
        let jan = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap();
        assert_eq!(QuotaWindow::Month.index(dec), 2025 * 12 + 11);
        assert_eq!(QuotaWindow::Month.index(jan), 2026 * 12);
        assert_eq!(
            QuotaWindow::Month.index(jan) - QuotaWindow::Month.index(dec),
            1,
            "Dec → Jan must advance by exactly one month index"
        );
    }

    #[tokio::test]
    async fn windows_and_keys_are_isolated() {
        let s = UsageState::new(None);
        s.set_usage("tok:g1", QuotaWindow::Day, 100).await;
        // Different window → independent counter.
        assert_eq!(s.read_usage("tok:g1", QuotaWindow::Month).await, 0);
        // Different counter key → independent.
        assert_eq!(s.read_usage("tok:g2", QuotaWindow::Day).await, 0);
        assert_eq!(s.read_usage("tok:g1", QuotaWindow::Day).await, 100);
    }

    #[tokio::test]
    async fn set_usage_force_overwrites_even_when_populated() {
        let s = UsageState::new(None);
        s.set_usage("tok:g1", QuotaWindow::Day, 100).await;
        assert_eq!(s.read_usage("tok:g1", QuotaWindow::Day).await, 100);
        // Force-baseline to the ledger value — unlike seed_if_absent, overwrites.
        s.set_usage("tok:g1", QuotaWindow::Day, 50).await;
        assert_eq!(s.read_usage("tok:g1", QuotaWindow::Day).await, 50);
    }

    /// The heal raises a warm, under-counted counter to the ledger and
    /// never lowers one that is ahead (reservations in flight).
    #[tokio::test]
    async fn raise_usage_to_heals_up_and_never_down() {
        let s = UsageState::new(None);
        s.set_usage("tok:g1", QuotaWindow::Day, 100).await;
        // Warm but behind the ledger (lost increments): raised.
        assert!(s.raise_usage_to("tok:g1", QuotaWindow::Day, 250).await);
        assert_eq!(s.read_usage("tok:g1", QuotaWindow::Day).await, 250);
        // Ahead of the ledger: untouched.
        assert!(!s.raise_usage_to("tok:g1", QuotaWindow::Day, 200).await);
        assert_eq!(s.read_usage("tok:g1", QuotaWindow::Day).await, 250);
        // Cold: seeded, as before.
        assert!(s.raise_usage_to("tok:g2", QuotaWindow::Day, 40).await);
        assert_eq!(s.read_usage("tok:g2", QuotaWindow::Day).await, 40);
    }

    #[tokio::test]
    async fn claim_shadow_baseline_true_without_redis() {
        let s = UsageState::new(None);
        assert!(s.try_claim_shadow_baseline().await);
    }

    #[tokio::test]
    async fn seed_if_absent_seeds_then_no_ops() {
        let s = UsageState::new(None);
        assert!(
            s.seed_usage_if_absent("tok:g1", QuotaWindow::Month, 500)
                .await
        );
        assert_eq!(s.read_usage("tok:g1", QuotaWindow::Month).await, 500);
        // Already populated → must not clobber.
        assert!(
            !s.seed_usage_if_absent("tok:g1", QuotaWindow::Month, 999)
                .await
        );
        assert_eq!(s.read_usage("tok:g1", QuotaWindow::Month).await, 500);
    }

    #[tokio::test]
    async fn seed_then_read_returns_seed() {
        let s = UsageState::new(None);
        s.seed_usage_if_absent("tok:g1", QuotaWindow::Day, 800)
            .await;
        assert_eq!(s.read_usage("tok:g1", QuotaWindow::Day).await, 800);
    }

    #[test]
    fn minute_key_format() {
        let key = UsageState::current_minute_key();
        // Should be a numeric string
        assert!(key.parse::<u64>().is_ok());
    }

    #[test]
    fn hour_key_format() {
        let key = UsageState::current_hour_key();
        assert!(key.parse::<u64>().is_ok());
        // Hour key should be smaller than minute key
        let minute: u64 = UsageState::current_minute_key().parse().unwrap();
        let hour: u64 = key.parse().unwrap();
        assert!(hour < minute);
    }

    #[test]
    fn day_key_format() {
        let key = UsageState::current_day_key();
        assert!(key.parse::<u64>().is_ok());
        let hour: u64 = UsageState::current_hour_key().parse().unwrap();
        let day: u64 = key.parse().unwrap();
        assert!(day < hour);
    }

    #[test]
    fn no_redis_reports_false() {
        let state = UsageState::new(None);
        assert!(!state.has_redis());
    }

    #[tokio::test]
    async fn run_pause_comes_off_the_age_without_redis() {
        // The per-pod fallback keeps the same clock semantics: time
        // recorded as parked on a human is not the run's age.
        let state = UsageState::new(None);
        let first = state.run_usage("t", "r").await.unwrap();
        assert!(first.started_ms > 0, "the clock starts on first touch");
        assert!(state.run_started_ms("t", "r").await.is_some());
        assert!(
            state.run_started_ms("t", "unknown").await.is_none(),
            "no clock, nothing to pause"
        );
        // Pretend the run has been open 10 s, 9 s of it awaiting a person.
        if let Some(mut e) = state.mem_run.get_mut("t:r") {
            e.started = Instant::now() - std::time::Duration::from_secs(10);
        }
        state.run_record_paused("t", "r", 9_000, RUN_TTL_SECS).await;
        let u = state.run_usage("t", "r").await.unwrap();
        assert_eq!(u.paused_ms, 9_000);
        assert_eq!(u.age_secs, 1);
        // Monotone: a smaller figure from a stale reconcile does not shrink it.
        state.run_record_paused("t", "r", 1_000, RUN_TTL_SECS).await;
        assert_eq!(state.run_usage("t", "r").await.unwrap().paused_ms, 9_000);
    }

    #[tokio::test]
    async fn no_redis_rpm_returns_zero() {
        let state = UsageState::new(None);
        assert_eq!(state.get_rpm("model-1").await, 0);
        assert!(!state.check_rpm("model-1", 100).await);
    }

    #[tokio::test]
    async fn no_redis_tpm_returns_zero() {
        let state = UsageState::new(None);
        assert_eq!(state.get_tpm("model-1").await, 0);
        assert!(!state.check_tpm("model-1", 10000).await);
    }

    #[tokio::test]
    async fn no_redis_cooldown_returns_false() {
        let state = UsageState::new(None);
        assert!(!state.is_cooled_down("model-1").await);
    }

    #[tokio::test]
    async fn no_redis_active_requests_returns_zero() {
        let state = UsageState::new(None);
        assert_eq!(state.get_active_requests("model-1").await, 0);
    }

    #[tokio::test]
    async fn no_redis_tool_calls_return_zero() {
        let state = UsageState::new(None);
        assert_eq!(state.get_tool_calls_hour("group-1").await, 0);
        assert_eq!(state.get_tool_calls_day("group-1").await, 0);
    }

    // -----------------------------------------------------------------------
    // MCP rate limiting — token bucket / quota / cooldown
    // -----------------------------------------------------------------------

    // --- Token-bucket math (pure) ---

    #[test]
    fn quota_window_secs() {
        assert_eq!(QuotaWindow::Hour.secs(), 3600);
        assert_eq!(QuotaWindow::Day.secs(), 86400);
    }
}

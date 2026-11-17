// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Admission: every limit a request touches, decided and reserved at once
//!
//! A limit is only a limit if concurrent requests cannot all pass it. Each
//! limiter used to be its own check, and most were *check-then-act*: read a
//! counter, compare, and charge the counter once the response came back.
//! Requests arriving together all read the same value and all passed, across
//! replicas the more so: run caps, tenant-wide and ancestor budgets, skill
//! quotas and tool-call limits could all be overrun that way.
//!
//! An [`Admission`] lists every dimension of every limit that applies to one
//! request — cumulative counters (quotas, budgets, run and session caps),
//! token buckets (per-minute rates), penalty-box cooldowns, in-flight
//! slots and sets of distinct members with a maximum size — and ONE script
//! decides all of them and reserves all of them, or none. Nothing a concurrent request does can fall between the check and the
//! charge, on this replica or another.
//!
//! ## Exact caps for tokens and cost
//!
//! A reservation that is only an *estimate* lets a call overshoot by
//! (actual − estimate). So a request is admitted against its WORST CASE: its
//! input upper bound plus the most output it may produce. The script also
//! computes the largest output that still fits every dimension
//! ([`Verdict::Admitted::out_tokens`]); the caller enforces it as the upstream
//! `max_tokens`, so the call cannot consume more than it reserved.
//!
//! ## Holds
//!
//! A request that will settle later (an LLM call) gets a *hold*: a record in
//! Redis of what it reserved, and a lease. The hold ends exactly once:
//!
//! * **settle** — the real usage replaces the reservation;
//! * **release** — the request never reached the provider (refused by a later
//!   gate, upstream error): everything comes back;
//! * **abandon** — it reached the provider but nobody reported usage (client
//!   hung up mid-stream, the replica died): the reservation is kept as spent.
//!   The worst case was reserved, so keeping it can over-count but never
//!   under-count.
//!
//! The lease is renewed while the replica holding the request is alive; a
//! replica that dies stops renewing and ANY replica's reaper abandons its
//! holds and frees their in-flight slots. Every hold's counter keys carry the
//! window index of the admission, so a call that straddles midnight settles
//! into the day it was admitted in.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use super::UsageState;
use super::mem_kv::MemTx;

/// How a dimension's amount is measured, at admission (worst case) and at
/// settle (actual).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Measure {
    /// One per request (request counts, call counts, executions).
    One,
    InTokens,
    OutTokens,
    TotalTokens,
    CostMicros,
    CostCents,
}

impl Measure {
    fn code(self) -> u8 {
        match self {
            Measure::One => 0,
            Measure::InTokens => 1,
            Measure::OutTokens => 2,
            Measure::TotalTokens => 3,
            Measure::CostMicros => 4,
            Measure::CostCents => 5,
        }
    }

    fn from_code(c: u8) -> Self {
        match c {
            1 => Measure::InTokens,
            2 => Measure::OutTokens,
            3 => Measure::TotalTokens,
            4 => Measure::CostMicros,
            5 => Measure::CostCents,
            _ => Measure::One,
        }
    }
}

/// One dimension of one limit.
#[derive(Debug, Clone, PartialEq)]
pub enum DimKind {
    /// A cumulative counter. Refuses when `current + amount > limit`; `None`
    /// is an accounting-only counter (reserved and settled, never refuses),
    /// which is how a scope's usage is kept exact even before anyone caps it.
    /// `key` is the full key, window index included.
    Counter {
        key: String,
        limit: Option<i64>,
        ttl_secs: u64,
    },
    /// A per-minute token bucket. Refuses when the refilled balance is below
    /// the call's charge. Unlike a counter it never squeezes the output, and
    /// it is charged the EXPECTED input (not the upper bound, which for a
    /// long prompt could exceed a whole minute's capacity and lock the caller
    /// out for good) and at most one capacity. Settle trues it up to the
    /// actual and carries any shortfall as debt (down to minus one
    /// capacity), so over any period the admitted rate is the configured
    /// rate — the way provider ITPM/OTPM limiters meter.
    Bucket {
        key: String,
        capacity: f64,
        refill_per_sec: f64,
    },
    /// A penalty box: refuses while the key exists.
    Cooldown { key: String },
    /// An in-flight slot: refuses when `max` holds are live. Needs a hold —
    /// the slot is the hold's, and goes when the hold ends.
    Slot { key: String, max: i64 },
    /// A set of distinct members with a maximum size (the distinct runs one
    /// credential started in a window). A member already in the set is
    /// admitted and charges nothing; a new one is admitted while the set
    /// holds fewer than `max` and is added in the same step, so "is this
    /// member new" is never read and then acted on. Refuses a new member
    /// when the set is full.
    ///
    /// Membership is final: it is not part of the request's hold, and ending
    /// the hold any way leaves it. Taking a member back when the request
    /// that added it is refused later would un-count a member another
    /// request has since been admitted on for free. `key` is the full key,
    /// window index included.
    Members {
        key: String,
        member: String,
        max: i64,
        ttl_secs: u64,
    },
}

impl DimKind {
    pub fn key(&self) -> &str {
        match self {
            DimKind::Counter { key, .. }
            | DimKind::Bucket { key, .. }
            | DimKind::Cooldown { key }
            | DimKind::Slot { key, .. }
            | DimKind::Members { key, .. } => key,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Dim {
    pub kind: DimKind,
    pub measure: Measure,
    /// Arm this cooldown (key, seconds) when THIS dimension refuses — the
    /// throughput penalty box.
    pub cooldown_on_refuse: Option<(String, u64)>,
}

impl Dim {
    pub fn counter(key: String, limit: Option<i64>, ttl_secs: u64, measure: Measure) -> Self {
        Self {
            kind: DimKind::Counter {
                key,
                limit,
                ttl_secs,
            },
            measure,
            cooldown_on_refuse: None,
        }
    }

    pub fn bucket(key: String, capacity: f64, refill_per_sec: f64, measure: Measure) -> Self {
        Self {
            kind: DimKind::Bucket {
                key,
                capacity,
                refill_per_sec,
            },
            measure,
            cooldown_on_refuse: None,
        }
    }

    pub fn cooldown(key: String) -> Self {
        Self {
            kind: DimKind::Cooldown { key },
            measure: Measure::One,
            cooldown_on_refuse: None,
        }
    }

    pub fn slot(key: String, max: i64) -> Self {
        Self {
            kind: DimKind::Slot { key, max },
            measure: Measure::One,
            cooldown_on_refuse: None,
        }
    }

    pub fn members(key: String, member: String, max: i64, ttl_secs: u64) -> Self {
        Self {
            kind: DimKind::Members {
                key,
                member,
                max,
                ttl_secs,
            },
            measure: Measure::One,
            cooldown_on_refuse: None,
        }
    }

    pub fn arming(mut self, cooldown: Option<(String, u64)>) -> Self {
        self.cooldown_on_refuse = cooldown.filter(|(_, s)| *s > 0);
        self
    }
}

/// What the request may consume, for the worst-case reservation.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Shape {
    /// Upper bound on input tokens: what a budget is reserved against.
    pub in_tokens: i64,
    /// Expected input tokens: what a per-minute bucket is charged at
    /// admission (see [`DimKind::Bucket`]).
    pub in_estimate: i64,
    /// Output tokens asked for (per choice). The admitted figure can be lower.
    pub out_tokens: i64,
    /// Below this many output tokens the request is refused rather than
    /// squeezed into a useless response.
    pub min_out: i64,
    /// Output choices produced per call (`n`): output is charged this many
    /// times over.
    pub choices: i64,
    /// Price, micro-USD per token. The same formula the usage ledger bills
    /// with (`estimate_llm_cost`), so worst case and actual agree.
    pub in_micros_per_token: f64,
    pub out_micros_per_token: f64,
}

impl Shape {
    /// A request whose every dimension is a count (MCP call, skill run, A2A
    /// invocation): nothing about it scales with tokens.
    pub fn count() -> Self {
        Self {
            in_tokens: 0,
            in_estimate: 0,
            out_tokens: 0,
            min_out: 0,
            choices: 1,
            in_micros_per_token: 0.0,
            out_micros_per_token: 0.0,
        }
    }

    /// `(base, per_out)`: the reservation for `measure` is
    /// `base + per_out * out_tokens`. `estimate` sizes the input from
    /// [`Self::in_estimate`] instead of the upper bound.
    fn linear(&self, measure: Measure, estimate: bool) -> (f64, f64) {
        let in_t = if estimate {
            self.in_estimate.max(0) as f64
        } else {
            self.in_tokens.max(0) as f64
        };
        let n = self.choices.max(1) as f64;
        let in_cost = in_t * self.in_micros_per_token.max(0.0);
        let out_cost = n * self.out_micros_per_token.max(0.0);
        match measure {
            Measure::One => (1.0, 0.0),
            Measure::InTokens => (in_t, 0.0),
            Measure::OutTokens => (0.0, n),
            Measure::TotalTokens => (in_t, n),
            Measure::CostMicros => (in_cost, out_cost),
            Measure::CostCents => (in_cost / 10_000.0, out_cost / 10_000.0),
        }
    }
}

/// What a settled request really consumed.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Actual {
    pub in_tokens: i64,
    pub out_tokens: i64,
    pub cost_micros: i64,
}

impl Actual {
    fn amount(&self, m: Measure) -> i64 {
        match m {
            Measure::One => 1,
            Measure::InTokens => self.in_tokens.max(0),
            Measure::OutTokens => self.out_tokens.max(0),
            Measure::TotalTokens => self.in_tokens.max(0) + self.out_tokens.max(0),
            Measure::CostMicros => self.cost_micros.max(0),
            Measure::CostCents => (self.cost_micros.max(0) + 9_999) / 10_000,
        }
    }
}

/// One request's admission.
#[derive(Debug, Clone)]
pub struct Admission {
    /// `Some` when the request settles later (it gets a hold); `None` for an
    /// admission that is final the moment it is granted.
    pub hold_id: Option<String>,
    pub dims: Vec<Dim>,
    pub shape: Shape,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Verdict {
    /// Every dimension reserved. `out_tokens` is the most output the call may
    /// produce; it is below `shape.out_tokens` when a cap squeezed it.
    Admitted { out_tokens: i64 },
    /// `dim` (index into `dims`) refused; nothing was reserved.
    Refused {
        dim: usize,
        /// The dimension's state when it refused (counter value, bucket
        /// balance, live slots, cooldown ms left).
        current: f64,
        retry_after_ms: u64,
    },
}

/// How a hold ends.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Finish {
    Settle(Actual),
    Release,
    Abandon,
    /// A count admission completed: what it reserved stays, its slots go,
    /// and the record is deleted (there is nothing left to correct).
    Commit,
}

/// A counter delta applied only when the request held nothing (it was never
/// admitted through a hold). Keeps settle a single entry point.
#[derive(Debug, Clone, PartialEq)]
pub struct Fallback {
    pub key: String,
    pub delta: i64,
    pub ttl_secs: u64,
}

/// Where part of a settled call's usage goes instead of the dimensions it
/// was admitted on (see [`UsageState::finish_hold_with`]).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Redirect {
    pub zero_matching: Option<String>,
    pub deltas: Vec<Fallback>,
}

/// How long a hold's lease runs without renewal before any replica's reaper
/// abandons it.
pub const HOLD_LEASE_MS: i64 = 60_000;
/// How often the live replica renews its leases.
pub const HOLD_RENEW_SECS: u64 = 15;
/// A hold nobody adopted (preflight admitted, the request died before
/// dispatch without passing a refusal path) is released after this.
pub const HOLD_UNADOPTED_MS: i64 = 120_000;
/// Hold records outlive their lease so a late settle can still correct the
/// counters after an abandon or release.
const HOLD_RECORD_MS: i64 = 2 * 3_600_000;
const HOLDS_ZSET: &str = "brutor:holds";
/// How long a released or abandoned hold keeps its dimensions for a late
/// settle. Late settles arrive within seconds; this bounds the memory.
const TOMBSTONE_MS: i64 = 1_800_000;

fn hold_key(id: &str) -> String {
    format!("brutor:hold:{id}")
}

// ---------------------------------------------------------------------------
// Lua
// ---------------------------------------------------------------------------

/// The admission script. ARGV[1] is the JSON spec built by
/// [`Admission::spec_json`]; KEYS are the dimension keys, then the cooldown
/// keys to arm, then the hold key and the holds zset.
///
/// Returns `{1, out}` or `{0, dim (1-based), current, retry_ms}`.
const ADMIT_LUA: &str = r#"
local s = cjson.decode(ARGV[1])
local now = tonumber(s.now)
local out = tonumber(s.out)
local bind = 0
local cur = {}
local function refuse(i, c, retry)
  local d = s.dims[i]
  if d and d.cd > 0 then
    redis.call('SET', KEYS[d.cd], '1', 'PX', d.cds)
  end
  if retry < 0 then retry = 0 end
  return {0, i, tostring(c), math.ceil(retry)}
end
local function bamt(d)
  local a = d.base + d.per * out
  if a > d.cap then a = d.cap end
  return a
end
-- Pass 1: read every dimension; counters and slots squeeze the output.
for i, d in ipairs(s.dims) do
  local key = KEYS[d.ki]
  if d.t == 'c' then
    local v = tonumber(redis.call('GET', key) or '0') or 0
    cur[i] = v
    local head = math.huge
    if d.lim >= 0 then head = d.lim - v end
    if d.per > 0 then
      local fit = math.floor((head - d.base) / d.per + 1e-9)
      if fit < out then out = fit; bind = i end
    elseif d.base > head + 1e-9 then
      return refuse(i, v, 0)
    end
  elseif d.t == 'b' then
    local h = redis.call('HMGET', key, 'tokens', 'ts')
    local tk = tonumber(h[1])
    local ts = tonumber(h[2])
    if tk == nil or ts == nil then tk = d.cap; ts = now end
    local el = (now - ts) / 1000.0
    if el < 0 then el = 0 end
    cur[i] = math.min(d.cap, tk + el * d.rf)
  elseif d.t == 'k' then
    local ttl = redis.call('PTTL', key)
    if ttl > 0 then return {0, i, tostring(ttl), ttl} end
  elseif d.t == 'n' then
    local n = redis.call('SCARD', key)
    cur[i] = n
    d.add = redis.call('SISMEMBER', key, d.mem) == 0
    if d.add and n + 1 > d.lim then return refuse(i, n, 0) end
  else
    redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
    local n = redis.call('ZCARD', key)
    cur[i] = n
    if n + 1 > d.lim then return refuse(i, n, 1000) end
  end
end
if bind > 0 and out < s.min_out then
  return refuse(bind, cur[bind] or 0, 0)
end
-- Pass 2: buckets, at the output the caps allow.
for i, d in ipairs(s.dims) do
  if d.t == 'b' then
    local a = bamt(d)
    if a > cur[i] + 1e-9 then
      local retry = 0
      if d.rf > 0 then retry = (a - cur[i]) / d.rf * 1000.0 end
      return refuse(i, cur[i], retry)
    end
  end
end
-- Apply.
local rec = {}
for i, d in ipairs(s.dims) do
  local key = KEYS[d.ki]
  if d.t == 'c' then
    local amt = math.ceil(d.base + d.per * out - 1e-9)
    if amt < 0 then amt = 0 end
    if amt > 0 then
      redis.call('INCRBY', key, string.format('%.0f', amt))
      redis.call('EXPIRE', key, d.ttl)
    end
    rec[#rec + 1] = {t = 'c', k = key, a = amt, m = d.m, ttl = d.ttl, cap = 0, rf = 0}
  elseif d.t == 'b' then
    local amt = bamt(d)
    redis.call('HSET', key, 'tokens', tostring(cur[i] - amt), 'ts', tostring(now))
    redis.call('EXPIRE', key, d.ttl)
    rec[#rec + 1] = {t = 'b', k = key, a = amt, m = d.m, ttl = d.ttl, cap = d.cap, rf = d.rf}
  elseif d.t == 's' then
    redis.call('ZADD', key, now + s.lease, s.hold)
    rec[#rec + 1] = {t = 's', k = key, a = 1, m = d.m, ttl = 0, cap = 0, rf = 0}
  elseif d.t == 'n' then
    -- Not in the hold's record: membership is final.
    if d.add then
      redis.call('SADD', key, d.mem)
      redis.call('EXPIRE', key, d.ttl)
    end
  end
end
if s.hold ~= '' then
  redis.call('SET', KEYS[s.hk], cjson.encode({st = 'live', d = rec}), 'PX', s.rec_ms)
  redis.call('ZADD', KEYS[s.hk + 1], now + s.lease, s.hold)
end
return {1, out}
"#;

/// Shared by the finish and reap scripts: end one hold.
///
/// `mode` = 'settle' | 'release' | 'abandon'. A live hold moves to a
/// tombstone that keeps its dimensions, so a LATE settle (after an abandon or
/// a release) can still put the counters right: after an abandon the counters
/// hold the reservation, after a release they hold nothing, and the settle
/// adds the difference to the actual. A settled tombstone makes a duplicate
/// settle a no-op.
macro_rules! finish_fn_lua {
    () => {
        r#"
local function act(m, a)
  if a == nil then return 0 end
  if m == 0 then return 1
  elseif m == 1 then return a.i
  elseif m == 2 then return a.o
  elseif m == 3 then return a.i + a.o
  elseif m == 4 then return a.c
  else return math.floor((a.c + 9999) / 10000) end
end
local function adj(k, delta, ttl)
  if delta == 0 then return end
  local v = redis.call('INCRBY', k, string.format('%.0f', delta))
  if v < 0 then redis.call('SET', k, '0', 'KEEPTTL') end
  if redis.call('TTL', k) < 0 then redis.call('EXPIRE', k, ttl) end
end
local function finish(hk, zk, id, mode, a, now, rec_ms, zm)
  local function amt(d)
    if zm ~= nil and zm ~= '' and string.find(d.k, zm, 1, true) then return act(d.m, nil) end
    return act(d.m, a)
  end
  local raw = redis.call('GET', hk)
  if not raw then return -1 end
  local r = cjson.decode(raw)
  local st = r.st
  if st == 'settled' then return 0 end
  if st == 'live' then
    local keep = (mode == 'abandon' or mode == 'commit')
    for _, d in ipairs(r.d) do
      if d.t == 'c' then
        if not keep then adj(d.k, amt(d) - d.a, d.ttl) end
      elseif d.t == 'b' then
        if not keep then
          local h = redis.call('HMGET', d.k, 'tokens', 'ts')
          local tk = tonumber(h[1])
          local ts = tonumber(h[2])
          if tk == nil or ts == nil then tk = d.cap; ts = now end
          local el = (now - ts) / 1000.0
          if el < 0 then el = 0 end
          tk = math.min(d.cap, tk + el * d.rf)
          tk = tk + d.a - amt(d)
          if tk > d.cap then tk = d.cap end
          if tk < -d.cap then tk = -d.cap end
          redis.call('HSET', d.k, 'tokens', tostring(tk), 'ts', tostring(now))
          redis.call('EXPIRE', d.k, d.ttl)
        end
      elseif d.t == 's' then
        redis.call('ZREM', d.k, id)
      end
    end
  else
    -- a tombstone: only counters still need putting right
    if mode == 'abandon' or mode == 'commit' then return 0 end
    if mode == 'release' and st ~= 'abandoned' then return 0 end
    for _, d in ipairs(r.d) do
      if d.t == 'c' then
        local held = 0
        if st == 'abandoned' then held = d.a end
        adj(d.k, amt(d) - held, d.ttl)
      end
    end
  end
  redis.call('ZREM', zk, id)
  if mode == 'commit' then
    redis.call('DEL', hk)
  elseif mode == 'settle' then
    redis.call('SET', hk, cjson.encode({st = 'settled'}), 'PX', 600000)
  else
    local nst = 'released'
    if mode == 'abandon' then nst = 'abandoned' end
    redis.call('SET', hk, cjson.encode({st = nst, d = r.d}), 'PX', 1800000)
  end
  return 1
end
"#
    };
}

/// End one hold. ARGV[1] JSON {id, mode, a: {i,o,c} | null, now, rec_ms,
/// fb: [{ki, d, ttl}]}; KEYS = hold key, holds zset, then fallback keys.
/// Fallback deltas apply only when there is no record at all (the request was
/// never admitted through a hold) and only on a settle.
const FINISH_LUA: &str = concat!(
    finish_fn_lua!(),
    r#"
local s = cjson.decode(ARGV[1])
local a = nil
if s.mode == 'settle' then a = s.a end
local r = finish(KEYS[1], KEYS[2], s.id, s.mode, a, tonumber(s.now), s.rec_ms, s.zm)
if r == -1 and s.mode == 'settle' then
  for _, f in ipairs(s.fb) do adj(KEYS[f.ki], f.d, f.ttl) end
end
if r == 1 and s.mode == 'settle' then
  for _, f in ipairs(s.al) do adj(KEYS[f.ki], f.d, f.ttl) end
end
return r
"#
);

/// Abandon every hold whose lease ran out. ARGV: now, rec_ms, batch.
const REAP_LUA: &str = concat!(
    finish_fn_lua!(),
    r#"
local now = tonumber(ARGV[1])
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'LIMIT', 0, tonumber(ARGV[3]))
local n = 0
for _, id in ipairs(ids) do
  local r = finish('brutor:hold:' .. id, KEYS[1], id, 'abandon', nil, now, tonumber(ARGV[2]), nil)
  if r == -1 then redis.call('ZREM', KEYS[1], id) end
  if r == 1 then n = n + 1 end
end
return n
"#
);

/// Extend the lease of live holds (and their in-flight slots). ARGV: now,
/// lease, rec_ms, ids...
const RENEW_LUA: &str = r#"
local now = tonumber(ARGV[1])
local exp = now + tonumber(ARGV[2])
local n = 0
for i = 4, #ARGV do
  local id = ARGV[i]
  local hk = 'brutor:hold:' .. id
  local raw = redis.call('GET', hk)
  if raw then
    local r = cjson.decode(raw)
    if r.st == 'live' then
      redis.call('ZADD', KEYS[1], 'XX', exp, id)
      for _, d in ipairs(r.d) do
        if d.t == 's' then redis.call('ZADD', d.k, 'XX', exp, id) end
      end
      redis.call('PEXPIRE', hk, tonumber(ARGV[3]))
      n = n + 1
    end
  end
end
return n
"#;

// ---------------------------------------------------------------------------
// Spec
// ---------------------------------------------------------------------------

impl Admission {
    /// Keys and JSON spec for [`ADMIT_LUA`].
    fn spec(&self, now_ms: i64) -> (Vec<String>, serde_json::Value) {
        let mut keys: Vec<String> = self.dims.iter().map(|d| d.kind.key().to_string()).collect();
        let mut dims = Vec::with_capacity(self.dims.len());
        for (i, d) in self.dims.iter().enumerate() {
            let est = matches!(d.kind, DimKind::Bucket { .. });
            let (base, per) = self.shape.linear(d.measure, est);
            let (cd, cds) = match &d.cooldown_on_refuse {
                Some((k, secs)) => {
                    keys.push(k.clone());
                    (keys.len(), (*secs as i64) * 1000)
                }
                None => (0, 0),
            };
            let member = match &d.kind {
                DimKind::Members { member, .. } => member.as_str(),
                _ => "",
            };
            let (t, lim, cap, rf, ttl) = match &d.kind {
                DimKind::Counter {
                    limit, ttl_secs, ..
                } => (
                    "c",
                    limit.map(|l| l as f64).unwrap_or(-1.0),
                    0.0,
                    0.0,
                    *ttl_secs as i64,
                ),
                DimKind::Bucket {
                    capacity,
                    refill_per_sec,
                    ..
                } => (
                    "b",
                    0.0,
                    *capacity,
                    *refill_per_sec,
                    bucket_ttl_secs(*capacity, *refill_per_sec),
                ),
                DimKind::Cooldown { .. } => ("k", 0.0, 0.0, 0.0, 0),
                DimKind::Slot { max, .. } => ("s", *max as f64, 0.0, 0.0, 0),
                DimKind::Members { max, ttl_secs, .. } => {
                    ("n", *max as f64, 0.0, 0.0, *ttl_secs as i64)
                }
            };
            dims.push(serde_json::json!({
                "t": t, "ki": i + 1, "lim": lim, "cap": cap, "rf": rf,
                "base": base, "per": per, "ttl": ttl, "m": d.measure.code(),
                "cd": cd, "cds": cds, "mem": member,
            }));
        }
        let hold = self.hold_id.clone().unwrap_or_default();
        keys.push(hold_key(&hold));
        let hk = keys.len();
        keys.push(HOLDS_ZSET.to_string());
        let spec = serde_json::json!({
            "now": now_ms,
            "out": self.shape.out_tokens.max(0),
            "min_out": self.shape.min_out.max(0),
            "lease": HOLD_LEASE_MS,
            "rec_ms": HOLD_RECORD_MS,
            "hold": hold,
            "hk": hk,
            "dims": dims,
        });
        (keys, spec)
    }
}

/// An idle bucket expires once it would have refilled anyway, so the next
/// call finds it full — the right outcome of a long idle period.
fn bucket_ttl_secs(capacity: f64, refill_per_sec: f64) -> i64 {
    if refill_per_sec > 0.0 {
        (capacity / refill_per_sec).ceil() as i64 + 10
    } else {
        3_600
    }
}

/// A hold this replica is responsible for.
#[derive(Debug)]
pub struct LiveHold {
    created: Instant,
    /// A guard owns the hold's lifetime (it was dispatched, or it is a count
    /// admission held in one scope).
    adopted: AtomicBool,
    /// The provider answered 2xx: an unreported end is spend, not a refund.
    billable: AtomicBool,
    /// What the hold was admitted for (the model of an LLM call), so a
    /// settle for something else can redirect.
    meta: std::sync::Mutex<Option<String>>,
}

impl UsageState {
    /// Decide and reserve every dimension of `adm`, or none.
    ///
    /// `Err` when Redis cannot be reached or the script fails: the caller
    /// refuses (fail closed). A slot dimension without a hold id is a
    /// programming error and is refused the same way.
    pub async fn admit(&self, adm: &Admission) -> Result<Verdict, redis::RedisError> {
        if adm.hold_id.is_none()
            && adm
                .dims
                .iter()
                .any(|d| matches!(d.kind, DimKind::Slot { .. }))
        {
            return Err(redis::RedisError::from((
                redis::ErrorKind::ClientError,
                "an in-flight slot needs a hold",
            )));
        }
        // Two dimensions on one key would each be judged against the same
        // pre-admission value and could jointly pass a limit neither may.
        let mut seen = std::collections::HashSet::new();
        if let Some(dup) = adm
            .dims
            .iter()
            .map(|d| d.kind.key())
            .find(|k| !seen.insert(*k))
        {
            tracing::error!(key = dup, "admission names one key twice; refusing");
            return Err(redis::RedisError::from((
                redis::ErrorKind::ClientError,
                "an admission names one key twice",
            )));
        }
        if adm.dims.is_empty() {
            return Ok(Verdict::Admitted {
                out_tokens: adm.shape.out_tokens.max(0),
            });
        }
        let now = super::now_epoch_ms();
        let verdict = match self.redis.as_ref() {
            Some(redis) => {
                let (keys, spec) = adm.spec(now);
                let lua = redis::Script::new(ADMIT_LUA);
                let mut call = lua.prepare_invoke();
                for k in &keys {
                    call.key(k);
                }
                call.arg(spec.to_string());
                let mut conn = redis.clone();
                // A caller that goes away while the script is on its way
                // (a client hang-up) would leave a hold Redis made and
                // nothing here owns: its slots taken until the lease runs
                // out, its counts kept by the reaper. Until the answer is
                // read, dropping this future releases the hold.
                let unowned = adm
                    .hold_id
                    .as_ref()
                    .map(|id| ReleaseIfDropped::new(redis.clone(), id.clone()));
                let v: Vec<redis::Value> = call.invoke_async(&mut conn).await?;
                if let Some(guard) = unowned {
                    guard.disarm();
                }
                parse_verdict(&v)?
            }
            None => {
                let mut tx = self.mem.lock(now);
                mem_admit(&mut tx, adm)
            }
        };
        if let (Verdict::Admitted { .. }, Some(id)) = (&verdict, &adm.hold_id) {
            self.live_holds.insert(
                id.clone(),
                Arc::new(LiveHold {
                    created: Instant::now(),
                    adopted: AtomicBool::new(false),
                    billable: AtomicBool::new(false),
                    meta: std::sync::Mutex::new(None),
                }),
            );
        }
        Ok(verdict)
    }

    /// End a hold. Returns `Ok(true)` when it acted on a record.
    pub async fn finish_hold(
        &self,
        hold_id: &str,
        how: Finish,
        fallback: &[Fallback],
    ) -> Result<bool, redis::RedisError> {
        self.finish_hold_with(hold_id, how, fallback, &Redirect::default())
            .await
    }

    /// [`Self::finish_hold`] for a call whose usage belongs partly to other
    /// keys than it was admitted on: dimensions whose key contains
    /// `redirect.zero_matching` settle to zero (as if released), and
    /// `redirect.deltas` are booked in their place — e.g. a call that fell
    /// back from the model it was admitted for to another one.
    pub async fn finish_hold_with(
        &self,
        hold_id: &str,
        how: Finish,
        fallback: &[Fallback],
        redirect: &Redirect,
    ) -> Result<bool, redis::RedisError> {
        self.live_holds.remove(hold_id);
        let now = super::now_epoch_ms();
        let (mode, actual) = match how {
            Finish::Settle(a) => ("settle", Some(a)),
            Finish::Release => ("release", None),
            Finish::Abandon => ("abandon", None),
            Finish::Commit => ("commit", None),
        };
        match self.redis.as_ref() {
            Some(redis) => {
                let r =
                    finish_on_redis(redis, hold_id, mode, actual, fallback, redirect, now).await?;
                Ok(r == 1)
            }
            None => {
                let mut tx = self.mem.lock(now);
                let r = mem_finish(&mut tx, hold_id, how, redirect.zero_matching.as_deref());
                if let Finish::Settle(_) = how {
                    let book = match r {
                        -1 => fallback,
                        1 => redirect.deltas.as_slice(),
                        _ => &[],
                    };
                    for f in book {
                        tx.incr_floor(&f.key, f.delta, Some(f.ttl_secs as i64 * 1000), false);
                    }
                }
                Ok(r == 1)
            }
        }
    }

    /// End a hold, logging (not propagating) a failure: for Drop paths and
    /// fire-and-forget settles, where there is no caller left to refuse. A
    /// hold that could not be ended stays leased until this replica stops
    /// renewing it, then the reaper abandons it — an over-count, never an
    /// under-count.
    pub async fn finish_hold_logged(&self, hold_id: &str, how: Finish, fallback: &[Fallback]) {
        if let Err(e) = self.finish_hold(hold_id, how, fallback).await {
            tracing::error!(hold_id, error = %e, ?how, "admission hold could not be ended; the reaper will abandon it");
        }
    }

    /// Renew every hold this replica still owns, and release the ones that
    /// were admitted but never dispatched. Called on a timer.
    pub async fn renew_live_holds(&self) -> Result<usize, redis::RedisError> {
        let mut stale = Vec::new();
        let mut ids = Vec::new();
        for e in self.live_holds.iter() {
            let age = e.value().created.elapsed().as_millis() as i64;
            if !e.value().adopted.load(Ordering::Acquire) && age > HOLD_UNADOPTED_MS {
                stale.push(e.key().clone());
            } else {
                ids.push(e.key().clone());
            }
        }
        for id in stale {
            self.finish_hold_logged(&id, Finish::Release, &[]).await;
        }
        if ids.is_empty() {
            return Ok(0);
        }
        let now = super::now_epoch_ms();
        match self.redis.as_ref() {
            Some(redis) => {
                let lua = redis::Script::new(RENEW_LUA);
                let mut call = lua.prepare_invoke();
                call.key(HOLDS_ZSET);
                call.arg(now).arg(HOLD_LEASE_MS).arg(HOLD_RECORD_MS);
                for id in &ids {
                    call.arg(id);
                }
                let mut conn = redis.clone();
                let n: i64 = call.invoke_async(&mut conn).await?;
                Ok(n as usize)
            }
            None => {
                let mut tx = self.mem.lock(now);
                Ok(mem_renew(&mut tx, &ids))
            }
        }
    }

    /// Abandon the holds of replicas that stopped renewing. Any replica may
    /// run it; each hold is ended once.
    pub async fn reap_expired_holds(&self) -> Result<usize, redis::RedisError> {
        let now = super::now_epoch_ms();
        match self.redis.as_ref() {
            Some(redis) => {
                let mut conn = redis.clone();
                let n: i64 = redis::Script::new(REAP_LUA)
                    .key(HOLDS_ZSET)
                    .arg(now)
                    .arg(HOLD_RECORD_MS)
                    .arg(200)
                    .invoke_async(&mut conn)
                    .await?;
                Ok(n as usize)
            }
            None => {
                let mut tx = self.mem.lock(now);
                let ids = tx.zrange_upto(HOLDS_ZSET, now, 200);
                let mut n = 0;
                for id in ids {
                    match mem_finish(&mut tx, &id, Finish::Abandon, None) {
                        1 => n += 1,
                        -1 => tx.zrem(HOLDS_ZSET, &id),
                        _ => {}
                    }
                }
                Ok(n)
            }
        }
    }

    /// A counter's current value (0 when absent). For tests and diagnostics;
    /// enforcement never reads-then-decides — it admits.
    pub async fn counter_value(&self, key: &str) -> Result<i64, redis::RedisError> {
        match self.redis.as_ref() {
            Some(redis) => {
                let mut conn = redis.clone();
                let v: Option<i64> = redis::cmd("GET").arg(key).query_async(&mut conn).await?;
                Ok(v.unwrap_or(0))
            }
            None => Ok(self.mem.lock(super::now_epoch_ms()).get_int(key)),
        }
    }

    /// Record what a live hold was admitted for (see [`LiveHold`]).
    pub fn set_hold_meta(&self, hold_id: &str, meta: &str) {
        if let Some(h) = self.live_holds.get(hold_id) {
            if let Ok(mut m) = h.meta.lock() {
                *m = Some(meta.to_string());
            }
        }
    }

    /// What a live hold was admitted for, if recorded.
    pub fn hold_meta(&self, hold_id: &str) -> Option<String> {
        self.live_holds
            .get(hold_id)
            .and_then(|h| h.meta.lock().ok().and_then(|m| m.clone()))
    }

    /// Take a hold out of this replica's registry NOW, before its finish is
    /// spawned: a guard dropped after this point leaves it alone, so the
    /// settle and the guard cannot race to end it two ways.
    pub fn forget_live_hold(&self, hold_id: &str) {
        self.live_holds.remove(hold_id);
    }

    /// A bucket's balance after its lazy refill. Test support.
    #[cfg(any(test, feature = "db-tests"))]
    pub async fn bucket_balance_for_test(
        &self,
        key: &str,
        capacity: f64,
        refill_per_sec: f64,
    ) -> f64 {
        match self.redis.as_ref() {
            Some(redis) => {
                let mut conn = redis.clone();
                let v: Vec<Option<f64>> = redis::cmd("HMGET")
                    .arg(key)
                    .arg("tokens")
                    .arg("ts")
                    .query_async(&mut conn)
                    .await
                    .unwrap_or_else(|e| panic!("bucket {key} could not be read: {e}"));
                match (v.first().copied().flatten(), v.get(1).copied().flatten()) {
                    (Some(tk), Some(ts)) => {
                        let el = ((super::now_epoch_ms() as f64 - ts) / 1000.0).max(0.0);
                        (tk + el * refill_per_sec).min(capacity)
                    }
                    _ => capacity,
                }
            }
            None => self
                .mem
                .lock(super::now_epoch_ms())
                .bucket(key, capacity, refill_per_sec),
        }
    }

    /// Whether this replica still owns `hold_id` (tests and diagnostics).
    pub fn holds_live(&self, hold_id: &str) -> bool {
        self.live_holds.contains_key(hold_id)
    }

    /// Hand a hold to a guard: from now on the guard's Drop decides how it
    /// ends if nobody settles it. Takes the proof that the hold was
    /// admitted (`limits::Held`), which only a successful admission under a
    /// hold yields: there is no guard for an admission that was refused.
    pub fn adopt_hold(self: &Arc<Self>, held: crate::services::limits::Held) -> HoldGuard {
        let hold_id = held.id();
        if let Some(h) = self.live_holds.get(hold_id) {
            h.adopted.store(true, Ordering::Release);
        }
        HoldGuard {
            usage_state: self.clone(),
            hold_id: Some(hold_id.to_string()),
        }
    }

    /// The provider answered 2xx for this hold's request.
    pub fn mark_hold_billable(&self, hold_id: &str) {
        if let Some(h) = self.live_holds.get(hold_id) {
            h.billable.store(true, Ordering::Release);
        }
    }
}

/// Owns a hold until it is settled. Drop ends a hold nobody settled: released
/// when the provider never answered 2xx, abandoned (kept as spent) when it
/// did.
pub struct HoldGuard {
    usage_state: Arc<UsageState>,
    hold_id: Option<String>,
}

impl HoldGuard {
    pub fn hold_id(&self) -> Option<&str> {
        self.hold_id.as_deref()
    }

    pub fn mark_billable(&self) {
        if let Some(id) = &self.hold_id {
            self.usage_state.mark_hold_billable(id);
        }
    }

    /// The request left for the upstream: from now on a guard dropped
    /// without an end abandons the hold (its counts stay spent, its
    /// in-flight slots are freed) instead of releasing it. In-process only.
    pub fn mark_dispatched(&self) {
        self.mark_billable();
    }

    /// End the hold now as a completed count (MCP call, skill run): the units
    /// it reserved stay spent.
    pub async fn commit(mut self) {
        if let Some(id) = self.hold_id.take() {
            self.usage_state
                .finish_hold_logged(&id, Finish::Commit, &[])
                .await;
        }
    }

    /// Give everything back now (a later gate refused the request).
    pub async fn release(mut self) {
        if let Some(id) = self.hold_id.take() {
            self.usage_state
                .finish_hold_logged(&id, Finish::Release, &[])
                .await;
        }
    }
}

impl Drop for HoldGuard {
    fn drop(&mut self) {
        let Some(id) = self.hold_id.take() else {
            return;
        };
        // Settled already: the settle removed it from the registry.
        let Some((_, live)) = self.usage_state.live_holds.remove(&id) else {
            return;
        };
        let how = if live.billable.load(Ordering::Acquire) {
            Finish::Abandon
        } else {
            Finish::Release
        };
        let us = self.usage_state.clone();
        match tokio::runtime::Handle::try_current() {
            Ok(h) => {
                h.spawn(async move { us.finish_hold_logged(&id, how, &[]).await });
            }
            // No runtime (a test dropping a guard outside one): the lease
            // runs out and the reaper ends it.
            Err(_) => {
                tracing::warn!(hold_id = %id, "hold dropped outside a runtime; left to the reaper")
            }
        }
    }
}

/// Run [`FINISH_LUA`] for one hold (`mode`: settle | release | abandon |
/// commit).
async fn finish_on_redis(
    redis: &redis::aio::ConnectionManager,
    hold_id: &str,
    mode: &str,
    actual: Option<Actual>,
    fallback: &[Fallback],
    redirect: &Redirect,
    now: i64,
) -> Result<i64, redis::RedisError> {
    let mut keys = vec![hold_key(hold_id), HOLDS_ZSET.to_string()];
    let mut push = |list: &[Fallback]| -> Vec<serde_json::Value> {
        list.iter()
            .map(|f| {
                keys.push(f.key.clone());
                serde_json::json!({"ki": keys.len(), "d": f.delta, "ttl": f.ttl_secs})
            })
            .collect()
    };
    let fb = push(fallback);
    let al = push(&redirect.deltas);
    let spec = serde_json::json!({
        "id": hold_id, "mode": mode, "now": now, "rec_ms": HOLD_RECORD_MS,
        "a": actual.map(|a| serde_json::json!({"i": a.in_tokens.max(0), "o": a.out_tokens.max(0), "c": a.cost_micros.max(0)})),
        "fb": fb,
        "al": al,
        "zm": redirect.zero_matching.clone().unwrap_or_default(),
    });
    let lua = redis::Script::new(FINISH_LUA);
    let mut call = lua.prepare_invoke();
    for k in &keys {
        call.key(k);
    }
    call.arg(spec.to_string());
    let mut conn = redis.clone();
    call.invoke_async(&mut conn).await
}

/// Armed while an admission under a hold waits for Redis' answer: dropped
/// armed (the caller went away), it releases the hold, on a clone of the
/// same multiplexed connection, so after the admission if Redis ran it (and
/// as a no-op if it never arrived).
struct ReleaseIfDropped {
    redis: Option<redis::aio::ConnectionManager>,
    hold_id: String,
}

impl ReleaseIfDropped {
    fn new(redis: redis::aio::ConnectionManager, hold_id: String) -> Self {
        Self {
            redis: Some(redis),
            hold_id,
        }
    }

    fn disarm(mut self) {
        self.redis = None;
    }
}

impl Drop for ReleaseIfDropped {
    fn drop(&mut self) {
        let Some(redis) = self.redis.take() else {
            return;
        };
        let id = std::mem::take(&mut self.hold_id);
        match tokio::runtime::Handle::try_current() {
            Ok(h) => {
                h.spawn(async move {
                    let now = super::now_epoch_ms();
                    if let Err(e) =
                        finish_on_redis(&redis, &id, "release", None, &[], &Redirect::default(), now)
                            .await
                    {
                        tracing::error!(hold_id = %id, error = %e, "abandoned admission could not be released; its lease will run out");
                    }
                });
            }
            Err(_) => {
                tracing::warn!(hold_id = %id, "admission dropped outside a runtime; its lease will run out")
            }
        }
    }
}

fn parse_verdict(v: &[redis::Value]) -> Result<Verdict, redis::RedisError> {
    let int = |x: &redis::Value| -> Option<i64> {
        match x {
            redis::Value::Int(n) => Some(*n),
            redis::Value::BulkString(b) => std::str::from_utf8(b).ok()?.parse().ok(),
            redis::Value::SimpleString(s) => s.parse().ok(),
            _ => None,
        }
    };
    let float = |x: &redis::Value| -> Option<f64> {
        match x {
            redis::Value::Int(n) => Some(*n as f64),
            redis::Value::BulkString(b) => std::str::from_utf8(b).ok()?.parse().ok(),
            redis::Value::SimpleString(s) => s.parse().ok(),
            _ => None,
        }
    };
    let bad = || {
        redis::RedisError::from((
            redis::ErrorKind::TypeError,
            "admission script returned an unexpected shape",
        ))
    };
    match v {
        [ok, out] if int(ok) == Some(1) => Ok(Verdict::Admitted {
            out_tokens: int(out).ok_or_else(bad)?,
        }),
        [ok, dim, cur, retry] if int(ok) == Some(0) => Ok(Verdict::Refused {
            dim: (int(dim).ok_or_else(bad)? - 1).max(0) as usize,
            current: float(cur).unwrap_or(0.0),
            retry_after_ms: int(retry).unwrap_or(0).max(0) as u64,
        }),
        _ => Err(bad()),
    }
}

// ---------------------------------------------------------------------------
// In-process twin — the same algorithm over the same keys, under one lock.
// ---------------------------------------------------------------------------

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
struct RecDim {
    t: String,
    k: String,
    a: f64,
    m: u8,
    ttl: i64,
    cap: f64,
    rf: f64,
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Debug)]
struct Rec {
    st: String,
    #[serde(default)]
    d: Vec<RecDim>,
}

fn mem_admit(tx: &mut MemTx<'_>, adm: &Admission) -> Verdict {
    let now = tx.now_ms;
    let mut out = adm.shape.out_tokens.max(0);
    let mut bind: Option<usize> = None;
    let mut cur: Vec<f64> = vec![0.0; adm.dims.len()];
    let lin: Vec<(f64, f64)> = adm
        .dims
        .iter()
        .map(|d| {
            adm.shape
                .linear(d.measure, matches!(d.kind, DimKind::Bucket { .. }))
        })
        .collect();
    let refuse = |tx: &mut MemTx<'_>, i: usize, c: f64, retry: f64| {
        if let Some((k, secs)) = &adm.dims[i].cooldown_on_refuse {
            tx.set_int(k, 1, Some(*secs as i64 * 1000));
        }
        Verdict::Refused {
            dim: i,
            current: c,
            retry_after_ms: retry.max(0.0).ceil() as u64,
        }
    };
    let bucket_amt = |i: usize, out: i64| -> f64 {
        let cap = match &adm.dims[i].kind {
            DimKind::Bucket { capacity, .. } => *capacity,
            _ => f64::INFINITY,
        };
        (lin[i].0 + lin[i].1 * out as f64).min(cap)
    };
    // Pass 1: read every dimension; counters and slots squeeze the output.
    for (i, d) in adm.dims.iter().enumerate() {
        let (base, per) = lin[i];
        match &d.kind {
            DimKind::Counter { key, limit, .. } => {
                let v = tx.get_int(key);
                cur[i] = v as f64;
                let head = limit.map(|l| (l - v) as f64).unwrap_or(f64::INFINITY);
                if per > 0.0 {
                    let fit = ((head - base) / per + 1e-9).floor();
                    if fit < out as f64 {
                        out = fit as i64;
                        bind = Some(i);
                    }
                } else if base > head + 1e-9 {
                    return refuse(tx, i, cur[i], 0.0);
                }
            }
            DimKind::Bucket {
                key,
                capacity,
                refill_per_sec,
            } => cur[i] = tx.bucket(key, *capacity, *refill_per_sec),
            DimKind::Cooldown { key } => {
                if let Some(ttl) = tx.pttl(key).filter(|t| *t > 0) {
                    return Verdict::Refused {
                        dim: i,
                        current: ttl as f64,
                        retry_after_ms: ttl as u64,
                    };
                }
            }
            DimKind::Slot { key, max } => {
                tx.zrem_upto(key, now);
                let n = tx.zcard(key);
                cur[i] = n as f64;
                if n + 1 > *max {
                    return refuse(tx, i, cur[i], 1000.0);
                }
            }
            DimKind::Members {
                key, member, max, ..
            } => {
                let n = tx.set_len(key);
                cur[i] = n as f64;
                if !tx.set_has(key, member) && n + 1 > *max {
                    return refuse(tx, i, cur[i], 0.0);
                }
            }
        }
    }
    if let Some(b) = bind.filter(|_| out < adm.shape.min_out.max(0)) {
        return refuse(tx, b, cur[b], 0.0);
    }
    // Pass 2: buckets, at the output the caps allow.
    for (i, d) in adm.dims.iter().enumerate() {
        if let DimKind::Bucket { refill_per_sec, .. } = &d.kind {
            let a = bucket_amt(i, out);
            if a > cur[i] + 1e-9 {
                let retry = if *refill_per_sec > 0.0 {
                    (a - cur[i]) / refill_per_sec * 1000.0
                } else {
                    0.0
                };
                return refuse(tx, i, cur[i], retry);
            }
        }
    }
    let hold = adm.hold_id.clone().unwrap_or_default();
    let mut rec = Vec::new();
    for (i, d) in adm.dims.iter().enumerate() {
        let (base, per) = lin[i];
        match &d.kind {
            DimKind::Counter { key, ttl_secs, .. } => {
                let amt = ((base + per * out as f64) - 1e-9).ceil().max(0.0) as i64;
                if amt > 0 {
                    tx.incr_floor(key, amt, Some(*ttl_secs as i64 * 1000), true);
                }
                rec.push(RecDim {
                    t: "c".into(),
                    k: key.clone(),
                    a: amt as f64,
                    m: d.measure.code(),
                    ttl: *ttl_secs as i64,
                    cap: 0.0,
                    rf: 0.0,
                });
            }
            DimKind::Bucket {
                key,
                capacity,
                refill_per_sec,
            } => {
                let amt = bucket_amt(i, out);
                let ttl = bucket_ttl_secs(*capacity, *refill_per_sec);
                tx.set_bucket(key, cur[i] - amt, ttl * 1000);
                rec.push(RecDim {
                    t: "b".into(),
                    k: key.clone(),
                    a: amt,
                    m: d.measure.code(),
                    ttl,
                    cap: *capacity,
                    rf: *refill_per_sec,
                });
            }
            DimKind::Slot { key, .. } => {
                tx.zadd(key, &hold, now + HOLD_LEASE_MS);
                rec.push(RecDim {
                    t: "s".into(),
                    k: key.clone(),
                    a: 1.0,
                    m: d.measure.code(),
                    ttl: 0,
                    cap: 0.0,
                    rf: 0.0,
                });
            }
            // Not in the hold's record: membership is final.
            DimKind::Members {
                key,
                member,
                ttl_secs,
                ..
            } => {
                if !tx.set_has(key, member) {
                    tx.set_add(key, member, *ttl_secs as i64 * 1000);
                }
            }
            DimKind::Cooldown { .. } => {}
        }
    }
    if !hold.is_empty() {
        let r = Rec {
            st: "live".into(),
            d: rec,
        };
        tx.set_str(
            &hold_key(&hold),
            serde_json::to_string(&r).unwrap_or_default(),
            Some(HOLD_RECORD_MS),
        );
        tx.zadd(HOLDS_ZSET, &hold, now + HOLD_LEASE_MS);
    }
    Verdict::Admitted { out_tokens: out }
}

/// Mirrors `finish()` in [`finish_fn_lua`]: -1 no record, 0 no-op, 1 acted.
fn mem_finish(tx: &mut MemTx<'_>, id: &str, how: Finish, zero_matching: Option<&str>) -> i32 {
    let hk = hold_key(id);
    let Some(raw) = tx.get_str(&hk) else {
        return -1;
    };
    let Ok(r) = serde_json::from_str::<Rec>(&raw) else {
        return -1;
    };
    let actual = match how {
        Finish::Settle(a) => Some(a),
        _ => None,
    };
    let act_m = |m: u8| actual.map(|a| a.amount(Measure::from_code(m))).unwrap_or(0);
    let amt = |d: &RecDim| match zero_matching {
        Some(z) if !z.is_empty() && d.k.contains(z) => 0,
        _ => act_m(d.m),
    };
    let adj = |tx: &mut MemTx<'_>, k: &str, delta: i64, ttl: i64| {
        if delta != 0 {
            tx.incr_floor(k, delta, Some(ttl * 1000), false);
        }
    };
    let keep = matches!(how, Finish::Abandon | Finish::Commit);
    match r.st.as_str() {
        "settled" => return 0,
        "live" => {
            for d in &r.d {
                match d.t.as_str() {
                    "c" if !keep => adj(tx, &d.k, amt(d) - d.a as i64, d.ttl),
                    "b" if !keep => {
                        let tk = tx.bucket(&d.k, d.cap, d.rf);
                        let tk = (tk + d.a - amt(d) as f64).clamp(-d.cap, d.cap);
                        tx.set_bucket(&d.k, tk, d.ttl * 1000);
                    }
                    "s" => tx.zrem(&d.k, id),
                    _ => {}
                }
            }
        }
        st => {
            if keep || (how == Finish::Release && st != "abandoned") {
                return 0;
            }
            for d in r.d.iter().filter(|d| d.t == "c") {
                let held = if st == "abandoned" { d.a as i64 } else { 0 };
                adj(tx, &d.k, amt(d) - held, d.ttl);
            }
        }
    }
    tx.zrem(HOLDS_ZSET, id);
    let (st, dims) = match how {
        Finish::Commit => {
            tx.del(&hk);
            return 1;
        }
        Finish::Abandon => ("abandoned", r.d),
        Finish::Release => ("released", r.d),
        Finish::Settle(_) => ("settled", Vec::new()),
    };
    let ttl = if st == "settled" {
        600_000
    } else {
        TOMBSTONE_MS
    };
    tx.set_str(
        &hk,
        serde_json::to_string(&Rec {
            st: st.into(),
            d: dims,
        })
        .unwrap_or_default(),
        Some(ttl),
    );
    1
}

fn mem_renew(tx: &mut MemTx<'_>, ids: &[String]) -> usize {
    let exp = tx.now_ms + HOLD_LEASE_MS;
    let mut n = 0;
    for id in ids {
        let hk = hold_key(id);
        let Some(r) = tx
            .get_str(&hk)
            .and_then(|raw| serde_json::from_str::<Rec>(&raw).ok())
        else {
            continue;
        };
        if r.st != "live" {
            continue;
        }
        tx.zadd_xx(HOLDS_ZSET, id, exp);
        for d in r.d.iter().filter(|d| d.t == "s") {
            tx.zadd_xx(&d.k, id, exp);
        }
        tx.expire(&hk, HOLD_RECORD_MS);
        n += 1;
    }
    n
}

#[cfg(test)]
mod tests {
    use super::*;

    fn us() -> Arc<UsageState> {
        Arc::new(UsageState::new(None))
    }

    fn cap_dim(key: &str, limit: i64, m: Measure) -> Dim {
        Dim::counter(key.to_string(), Some(limit), 3600, m)
    }

    fn llm_shape(in_t: i64, out: i64) -> Shape {
        Shape {
            in_tokens: in_t,
            in_estimate: in_t / 4,
            out_tokens: out,
            min_out: 1,
            choices: 1,
            in_micros_per_token: 1.0,
            out_micros_per_token: 4.0,
        }
    }

    #[tokio::test]
    async fn a_count_admission_stops_exactly_at_the_limit() {
        let s = us();
        let adm = Admission {
            hold_id: None,
            dims: vec![cap_dim("c:a", 3, Measure::One)],
            shape: Shape::count(),
        };
        let mut admitted = 0;
        for _ in 0..10 {
            if matches!(s.admit(&adm).await.unwrap(), Verdict::Admitted { .. }) {
                admitted += 1;
            }
        }
        assert_eq!(admitted, 3);
    }

    #[tokio::test]
    async fn the_output_is_squeezed_to_what_every_cap_still_allows() {
        let s = us();
        // 1000-token cap, 400 input: at most 600 output fit.
        let adm = Admission {
            hold_id: Some("h-squeeze".into()),
            dims: vec![cap_dim("tok:a", 1000, Measure::TotalTokens)],
            shape: llm_shape(400, 4096),
        };
        assert_eq!(
            s.admit(&adm).await.unwrap(),
            Verdict::Admitted { out_tokens: 600 }
        );
        // The whole worst case is held: nothing is left.
        let again = Admission {
            hold_id: Some("h-squeeze-2".into()),
            ..adm.clone()
        };
        assert!(matches!(
            s.admit(&again).await.unwrap(),
            Verdict::Refused { .. }
        ));
    }

    #[tokio::test]
    async fn a_cost_cap_squeezes_by_price() {
        let s = us();
        // 10_000 micro-USD cap; 1000 input at 1 µ$ = 1000 µ$; output 4 µ$ each:
        // (10_000 - 1000) / 4 = 2250 tokens.
        let adm = Admission {
            hold_id: Some("h-cost".into()),
            dims: vec![cap_dim("cost:a", 10_000, Measure::CostMicros)],
            shape: llm_shape(1000, 100_000),
        };
        assert_eq!(
            s.admit(&adm).await.unwrap(),
            Verdict::Admitted { out_tokens: 2250 }
        );
    }

    #[tokio::test]
    async fn below_the_minimum_output_the_request_is_refused() {
        let s = us();
        let mut shape = llm_shape(990, 4096);
        shape.min_out = 64;
        let adm = Admission {
            hold_id: Some("h-min".into()),
            dims: vec![cap_dim("tok:min", 1000, Measure::TotalTokens)],
            shape,
        };
        assert!(matches!(
            s.admit(&adm).await.unwrap(),
            Verdict::Refused { dim: 0, .. }
        ));
    }

    #[tokio::test]
    async fn a_refusal_reserves_nothing_on_any_dimension() {
        let s = us();
        let adm = Admission {
            hold_id: None,
            dims: vec![
                cap_dim("c:first", 10, Measure::One),
                cap_dim("c:full", 0, Measure::One),
            ],
            shape: Shape::count(),
        };
        assert!(matches!(
            s.admit(&adm).await.unwrap(),
            Verdict::Refused { dim: 1, .. }
        ));
        assert_eq!(s.mem.lock(0).get_int("c:first"), 0, "all or nothing");
    }

    #[tokio::test]
    async fn settle_replaces_the_reservation_with_the_actual() {
        let s = us();
        let adm = Admission {
            hold_id: Some("h-settle".into()),
            dims: vec![cap_dim("tok:s", 10_000, Measure::TotalTokens)],
            shape: llm_shape(100, 900),
        };
        s.admit(&adm).await.unwrap();
        assert_eq!(
            s.mem.lock(super::super::now_epoch_ms()).get_int("tok:s"),
            1000
        );
        let a = Actual {
            in_tokens: 80,
            out_tokens: 120,
            cost_micros: 0,
        };
        assert!(
            s.finish_hold("h-settle", Finish::Settle(a), &[])
                .await
                .unwrap()
        );
        assert_eq!(
            s.mem.lock(super::super::now_epoch_ms()).get_int("tok:s"),
            200
        );
        // A duplicate settle is a no-op.
        assert!(
            !s.finish_hold("h-settle", Finish::Settle(a), &[])
                .await
                .unwrap()
        );
        assert_eq!(
            s.mem.lock(super::super::now_epoch_ms()).get_int("tok:s"),
            200
        );
    }

    #[tokio::test]
    async fn release_gives_everything_back_and_abandon_keeps_it() {
        let s = us();
        for (id, how, left) in [("h-rel", Finish::Release, 0), ("h-ab", Finish::Abandon, 1)] {
            let key = format!("req:{id}");
            let adm = Admission {
                hold_id: Some(id.into()),
                dims: vec![cap_dim(&key, 5, Measure::One)],
                shape: Shape::count(),
            };
            s.admit(&adm).await.unwrap();
            s.finish_hold(id, how, &[]).await.unwrap();
            assert_eq!(
                s.mem.lock(super::super::now_epoch_ms()).get_int(&key),
                left,
                "{id}"
            );
        }
    }

    #[tokio::test]
    async fn a_late_settle_after_an_abandon_corrects_to_the_actual() {
        let s = us();
        let adm = Admission {
            hold_id: Some("h-late".into()),
            dims: vec![cap_dim("tok:late", 10_000, Measure::TotalTokens)],
            shape: llm_shape(100, 900),
        };
        s.admit(&adm).await.unwrap();
        s.finish_hold("h-late", Finish::Abandon, &[]).await.unwrap();
        assert_eq!(
            s.mem.lock(super::super::now_epoch_ms()).get_int("tok:late"),
            1000
        );
        let a = Actual {
            in_tokens: 100,
            out_tokens: 50,
            cost_micros: 0,
        };
        s.finish_hold("h-late", Finish::Settle(a), &[])
            .await
            .unwrap();
        assert_eq!(
            s.mem.lock(super::super::now_epoch_ms()).get_int("tok:late"),
            150
        );
    }

    #[tokio::test]
    async fn a_late_settle_after_a_release_books_the_full_actual() {
        let s = us();
        let adm = Admission {
            hold_id: Some("h-late-rel".into()),
            dims: vec![cap_dim("tok:late-rel", 10_000, Measure::TotalTokens)],
            shape: llm_shape(100, 900),
        };
        s.admit(&adm).await.unwrap();
        s.finish_hold("h-late-rel", Finish::Release, &[])
            .await
            .unwrap();
        let a = Actual {
            in_tokens: 70,
            out_tokens: 30,
            cost_micros: 0,
        };
        s.finish_hold("h-late-rel", Finish::Settle(a), &[])
            .await
            .unwrap();
        assert_eq!(
            s.mem
                .lock(super::super::now_epoch_ms())
                .get_int("tok:late-rel"),
            100
        );
    }

    #[tokio::test]
    async fn a_settle_without_any_hold_books_its_fallback() {
        let s = us();
        let fb = [Fallback {
            key: "tok:nohold".into(),
            delta: 42,
            ttl_secs: 60,
        }];
        let a = Actual::default();
        s.finish_hold("never-admitted", Finish::Settle(a), &fb)
            .await
            .unwrap();
        assert_eq!(
            s.mem
                .lock(super::super::now_epoch_ms())
                .get_int("tok:nohold"),
            42
        );
    }

    #[tokio::test]
    async fn a_slot_is_freed_by_any_end_and_by_an_expired_lease() {
        let s = us();
        let slot = |id: &str| Admission {
            hold_id: Some(id.into()),
            dims: vec![Dim::slot("slot:x".into(), 1)],
            shape: Shape::count(),
        };
        assert!(matches!(
            s.admit(&slot("s1")).await.unwrap(),
            Verdict::Admitted { .. }
        ));
        assert!(matches!(
            s.admit(&slot("s2")).await.unwrap(),
            Verdict::Refused { .. }
        ));
        s.finish_hold("s1", Finish::Abandon, &[]).await.unwrap();
        assert!(matches!(
            s.admit(&slot("s3")).await.unwrap(),
            Verdict::Admitted { .. }
        ));
        // A replica that dies: its lease lapses and the next admission
        // purges it.
        {
            let mut tx = s.mem.lock(0);
            tx.zadd("slot:x", "s3", 1);
        }
        assert!(matches!(
            s.admit(&slot("s4")).await.unwrap(),
            Verdict::Admitted { .. }
        ));
    }

    #[tokio::test]
    async fn the_reaper_abandons_only_expired_holds() {
        let s = us();
        let adm = |id: &str| Admission {
            hold_id: Some(id.into()),
            dims: vec![cap_dim("req:reap", 100, Measure::One)],
            shape: Shape::count(),
        };
        s.admit(&adm("fresh")).await.unwrap();
        s.admit(&adm("stale")).await.unwrap();
        {
            let mut tx = s.mem.lock(0);
            tx.zadd(HOLDS_ZSET, "stale", 1);
        }
        assert_eq!(s.reap_expired_holds().await.unwrap(), 1);
        // Abandoned = kept as spent: both units still counted.
        assert_eq!(
            s.mem.lock(super::super::now_epoch_ms()).get_int("req:reap"),
            2
        );
    }

    #[tokio::test]
    async fn a_bucket_charged_at_admission_is_refunded_to_the_actual() {
        let s = us();
        let adm = Admission {
            hold_id: Some("h-b".into()),
            dims: vec![Dim::bucket(
                "b:otpm".into(),
                1000.0,
                1000.0 / 60.0,
                Measure::OutTokens,
            )],
            shape: llm_shape(0, 600),
        };
        assert_eq!(
            s.admit(&adm).await.unwrap(),
            Verdict::Admitted { out_tokens: 600 }
        );
        // 400 left: a bucket never squeezes, it refuses until it refills.
        let adm2 = Admission {
            hold_id: Some("h-b2".into()),
            ..adm.clone()
        };
        assert!(matches!(
            s.admit(&adm2).await.unwrap(),
            Verdict::Refused { dim: 0, .. }
        ));
        s.finish_hold(
            "h-b",
            Finish::Settle(Actual {
                in_tokens: 0,
                out_tokens: 100,
                cost_micros: 0,
            }),
            &[],
        )
        .await
        .unwrap();
        let bal = s
            .mem
            .lock(super::super::now_epoch_ms())
            .bucket("b:otpm", 1000.0, 1000.0 / 60.0);
        assert!((900.0..=920.0).contains(&bal), "refunded 500, got {bal}");
    }

    #[tokio::test]
    async fn a_bucket_carries_an_underestimate_as_debt() {
        let s = us();
        // ITPM 1000; the call was expected to read 100 (charged), read 1900.
        let adm = Admission {
            hold_id: Some("h-debt".into()),
            dims: vec![Dim::bucket(
                "b:itpm".into(),
                1000.0,
                1000.0 / 60.0,
                Measure::InTokens,
            )],
            shape: llm_shape(400, 0),
        };
        assert!(matches!(
            s.admit(&adm).await.unwrap(),
            Verdict::Admitted { .. }
        ));
        s.finish_hold(
            "h-debt",
            Finish::Settle(Actual {
                in_tokens: 1900,
                out_tokens: 0,
                cost_micros: 0,
            }),
            &[],
        )
        .await
        .unwrap();
        let bal = s
            .mem
            .lock(super::super::now_epoch_ms())
            .bucket("b:itpm", 1000.0, 1000.0 / 60.0);
        assert!(bal < -800.0, "the shortfall is owed, not forgiven: {bal}");
        let next = Admission {
            hold_id: Some("h-debt-2".into()),
            ..adm.clone()
        };
        assert!(matches!(
            s.admit(&next).await.unwrap(),
            Verdict::Refused { .. }
        ));
    }

    #[tokio::test]
    async fn a_refusing_bucket_arms_its_cooldown() {
        let s = us();
        let d = Dim::bucket("b:rpm".into(), 1.0, 1.0 / 60.0, Measure::One)
            .arming(Some(("cd:scope".into(), 30)));
        let adm = Admission {
            hold_id: None,
            dims: vec![Dim::cooldown("cd:scope".into()), d],
            shape: Shape::count(),
        };
        assert!(matches!(
            s.admit(&adm).await.unwrap(),
            Verdict::Admitted { .. }
        ));
        assert!(matches!(
            s.admit(&adm).await.unwrap(),
            Verdict::Refused { dim: 1, .. }
        ));
        // Now the cooldown itself refuses.
        assert!(matches!(
            s.admit(&adm).await.unwrap(),
            Verdict::Refused { dim: 0, .. }
        ));
    }

    #[tokio::test]
    async fn a_dropped_guard_releases_unless_the_provider_answered() {
        let s = us();
        for (id, billable, left) in [("g-rel", false, 0), ("g-ab", true, 1)] {
            let key = format!("req:{id}");
            let adm = Admission {
                hold_id: Some(id.into()),
                dims: vec![cap_dim(&key, 5, Measure::One)],
                shape: Shape::count(),
            };
            s.admit(&adm).await.unwrap();
            let g = s.adopt_hold(crate::services::limits::Held::for_test(id));
            if billable {
                g.mark_billable();
            }
            drop(g);
            // Drop spawns the finish; let it run.
            for _ in 0..50 {
                tokio::task::yield_now().await;
            }
            assert_eq!(
                s.mem.lock(super::super::now_epoch_ms()).get_int(&key),
                left,
                "{id}"
            );
        }
    }

    #[tokio::test]
    async fn an_unadopted_hold_is_released_by_the_renew_pass() {
        let s = us();
        let adm = Admission {
            hold_id: Some("orphan".into()),
            dims: vec![cap_dim("req:orphan", 5, Measure::One)],
            shape: Shape::count(),
        };
        s.admit(&adm).await.unwrap();
        // Age it past the adoption deadline.
        if let Some(mut h) = s.live_holds.get_mut("orphan") {
            let old = Arc::new(LiveHold {
                created: Instant::now()
                    - std::time::Duration::from_millis(HOLD_UNADOPTED_MS as u64 + 1),
                adopted: AtomicBool::new(false),
                billable: AtomicBool::new(false),
                meta: std::sync::Mutex::new(None),
            });
            *h = old;
        }
        s.renew_live_holds().await.unwrap();
        assert_eq!(
            s.mem
                .lock(super::super::now_epoch_ms())
                .get_int("req:orphan"),
            0
        );
    }

    #[tokio::test]
    async fn a_fallback_settles_onto_the_model_that_ran() {
        let s = us();
        let adm = Admission {
            hold_id: Some("h-fb".into()),
            dims: vec![
                Dim::counter("tok:grp:g".into(), None, 3600, Measure::TotalTokens),
                Dim::counter(
                    "tok:grp:g:mdl:A".into(),
                    Some(10_000),
                    3600,
                    Measure::TotalTokens,
                ),
            ],
            shape: llm_shape(100, 900),
        };
        s.admit(&adm).await.unwrap();
        let redirect = Redirect {
            zero_matching: Some(":mdl:A".into()),
            deltas: vec![Fallback {
                key: "tok:grp:g:mdl:B".into(),
                delta: 150,
                ttl_secs: 3600,
            }],
        };
        let a = Actual {
            in_tokens: 100,
            out_tokens: 50,
            cost_micros: 0,
        };
        s.finish_hold_with("h-fb", Finish::Settle(a), &[], &redirect)
            .await
            .unwrap();
        let now = super::super::now_epoch_ms();
        let mut tx = s.mem.lock(now);
        assert_eq!(tx.get_int("tok:grp:g"), 150, "the group carries the actual");
        assert_eq!(
            tx.get_int("tok:grp:g:mdl:A"),
            0,
            "the model that did not run, nothing"
        );
        assert_eq!(
            tx.get_int("tok:grp:g:mdl:B"),
            150,
            "the model that ran, the actual"
        );
    }

    // -- a set of distinct members with a maximum size ------------------------

    fn member_adm(key: &str, member: &str, max: i64) -> Admission {
        Admission {
            hold_id: None,
            dims: vec![Dim::members(key.into(), member.into(), max, 3600)],
            shape: Shape::count(),
        }
    }

    fn admitted(v: &Verdict) -> bool {
        matches!(v, Verdict::Admitted { .. })
    }

    #[tokio::test]
    async fn a_member_in_the_set_is_free_and_a_new_one_takes_a_place() {
        let s = us();
        assert!(admitted(
            &s.admit(&member_adm("set:a", "r1", 2)).await.unwrap()
        ));
        // The same member again, any number of times: no place taken.
        for _ in 0..5 {
            assert!(admitted(
                &s.admit(&member_adm("set:a", "r1", 2)).await.unwrap()
            ));
        }
        assert!(admitted(
            &s.admit(&member_adm("set:a", "r2", 2)).await.unwrap()
        ));
        // The set is full: a third member is refused, at the size it stands.
        assert_eq!(
            s.admit(&member_adm("set:a", "r3", 2)).await.unwrap(),
            Verdict::Refused {
                dim: 0,
                current: 2.0,
                retry_after_ms: 0
            }
        );
        // ...and the members it holds are still admitted.
        assert!(admitted(
            &s.admit(&member_adm("set:a", "r1", 2)).await.unwrap()
        ));
        assert!(admitted(
            &s.admit(&member_adm("set:a", "r2", 2)).await.unwrap()
        ));
        let mut tx = s.mem.lock(super::super::now_epoch_ms());
        assert_eq!(tx.set_len("set:a"), 2);
        assert!(!tx.set_has("set:a", "r3"), "a refused member was added");
        // Another set is another count.
        drop(tx);
        assert!(admitted(
            &s.admit(&member_adm("set:b", "r3", 2)).await.unwrap()
        ));
    }

    #[tokio::test]
    async fn a_member_is_added_with_the_admissions_other_dimensions_or_not_at_all() {
        let s = us();
        // The counter is full: the admission is refused, and adds no member.
        let full = cap_dim("c:full", 1, Measure::One);
        s.admit(&Admission {
            hold_id: None,
            dims: vec![full.clone()],
            shape: Shape::count(),
        })
        .await
        .unwrap();
        for dims in [
            vec![
                Dim::members("set:all".into(), "r1".into(), 5, 3600),
                full.clone(),
            ],
            vec![
                full.clone(),
                Dim::members("set:all".into(), "r1".into(), 5, 3600),
            ],
        ] {
            let v = s
                .admit(&Admission {
                    hold_id: None,
                    dims,
                    shape: Shape::count(),
                })
                .await
                .unwrap();
            assert!(matches!(v, Verdict::Refused { .. }), "{v:?}");
        }
        assert_eq!(
            s.mem.lock(super::super::now_epoch_ms()).set_len("set:all"),
            0
        );

        // The set is full: the admission is refused, and the counter beside
        // it is not charged.
        assert!(admitted(
            &s.admit(&member_adm("set:one", "r1", 1)).await.unwrap()
        ));
        let v = s
            .admit(&Admission {
                hold_id: None,
                dims: vec![
                    cap_dim("c:beside", 10, Measure::One),
                    Dim::members("set:one".into(), "r2".into(), 1, 3600),
                ],
                shape: Shape::count(),
            })
            .await
            .unwrap();
        assert_eq!(
            v,
            Verdict::Refused {
                dim: 1,
                current: 1.0,
                retry_after_ms: 0
            }
        );
        assert_eq!(s.counter_value("c:beside").await.unwrap(), 0);
    }

    #[tokio::test]
    async fn membership_outlives_the_hold_it_was_admitted_under() {
        for how in [Finish::Release, Finish::Abandon, Finish::Commit] {
            let s = us();
            let adm = Admission {
                hold_id: Some("h-member".into()),
                dims: vec![
                    Dim::members("set:hold".into(), "r1".into(), 1, 3600),
                    cap_dim("c:hold", 10, Measure::One),
                ],
                shape: Shape::count(),
            };
            assert!(admitted(&s.admit(&adm).await.unwrap()));
            s.finish_hold("h-member", how, &[]).await.unwrap();
            let kept = if how == Finish::Release { 0 } else { 1 };
            assert_eq!(s.counter_value("c:hold").await.unwrap(), kept, "{how:?}");
            // The member stays: the set is still full for another one.
            assert!(
                !admitted(&s.admit(&member_adm("set:hold", "r2", 1)).await.unwrap()),
                "{how:?} took the member back"
            );
            assert!(admitted(
                &s.admit(&member_adm("set:hold", "r1", 1)).await.unwrap()
            ));
        }
    }

    #[tokio::test]
    async fn a_member_set_lapses_with_its_window() {
        let s = us();
        let now = super::super::now_epoch_ms();
        {
            let mut tx = s.mem.lock(now);
            let adm = Admission {
                hold_id: None,
                dims: vec![Dim::members("set:ttl".into(), "r1".into(), 1, 10)],
                shape: Shape::count(),
            };
            assert!(admitted(&mem_admit(&mut tx, &adm)));
            assert_eq!(tx.pttl("set:ttl"), Some(10_000));
        }
        // Inside its lifetime the set is full; after it, empty again.
        let mut tx = s.mem.lock(now + 9_999);
        assert!(!admitted(&mem_admit(
            &mut tx,
            &member_adm("set:ttl", "r2", 1)
        )));
        drop(tx);
        let mut tx = s.mem.lock(now + 10_000);
        assert!(admitted(&mem_admit(
            &mut tx,
            &member_adm("set:ttl", "r2", 1)
        )));
        assert_eq!(tx.set_len("set:ttl"), 1);
    }

    #[test]
    fn a_member_set_is_keyed_and_specified_like_every_other_dimension() {
        let d = Dim::members("set:k".into(), "run-1".into(), 4, 7200);
        assert_eq!(d.kind.key(), "set:k");
        let adm = Admission {
            hold_id: None,
            dims: vec![d],
            shape: Shape::count(),
        };
        let (keys, spec) = adm.spec(1_000);
        assert_eq!(keys[0], "set:k");
        let dim = &spec["dims"][0];
        assert_eq!(dim["t"], "n");
        assert_eq!(dim["mem"], "run-1");
        assert_eq!(dim["lim"], 4.0);
        assert_eq!(dim["ttl"], 7200);
    }
}

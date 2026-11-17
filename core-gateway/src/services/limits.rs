// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Limit dimensions
//!
//! Every configured limit becomes one or more [`LimitDim`]s: an admission
//! dimension (`usage_state::Dim`) paired with what to say if it refuses. A
//! request is admitted against ALL of its dimensions in one atomic step
//! ([`admit`]), so no limit is check-then-act and none can be passed by
//! requests that arrive together (see `usage_state::admission`).
//!
//! The builders here translate the operator-facing limit shapes — MCP/LLM/A2A
//! `frequency`, LLM `throughput`, cumulative `budget`/`tokens`/`requests` and
//! `execution` quotas — into dimensions under the SAME counter keys the
//! limiters used before, so counters in flight carry over a deploy.

use std::time::{SystemTime, UNIX_EPOCH};

use crate::models::proxy::UsageEnforcementResult;
use crate::services::cache::usage_state::{
    Admission, Dim, Measure, QuotaWindow, Shape, UsageState, Verdict,
};
use crate::services::usage::McpRateLimitBlock;

/// The identity of an admission hold: one request's own.
///
/// It names the tenant, the surface that admitted and a part the gateway
/// generates per admission, so no two admissions ever share a hold record —
/// two requests of one tenant, or of two tenants, in flight together
/// included. The correlation id is NOT part of it: a client may send its
/// own, and may send the same one on several requests; a hold named from it
/// is one record that every such request admits into, releases and settles.
/// [`HoldId::new`] is the only way to make one, and [`admit`] takes nothing
/// else (`tests/limits_guard.rs` pins both).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HoldId(String);

impl HoldId {
    /// A new hold identity for a request of `tenant_id` admitted on
    /// `surface` (a short constant naming the admitting gate).
    pub fn new(tenant_id: &str, surface: &'static str) -> Self {
        Self(format!(
            "{tenant_id}:{surface}:{}",
            uuid::Uuid::new_v4().simple()
        ))
    }

    /// A hold under a name the test chooses, so it can end the hold by
    /// that name. Not compiled into the gateway.
    #[cfg(any(test, feature = "db-tests"))]
    pub fn for_test(name: &str) -> Self {
        Self(name.to_string())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    pub fn into_string(self) -> String {
        self.0
    }
}

impl std::fmt::Display for HoldId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// The proof that an admission reserved under a hold: [`admit`] makes one
/// when, and only when, it admitted under a [`HoldId`]. A guard is adopted
/// from it and from nothing else (`UsageState::adopt_hold`), so a request
/// that was refused, or that reserved nothing under a hold, has no guard to
/// end: there is no hold of its to release, commit or settle.
#[derive(Debug)]
pub struct Held(String);

impl Held {
    pub fn id(&self) -> &str {
        &self.0
    }

    pub fn into_id(self) -> String {
        self.0
    }

    /// The proof for a hold a test admitted below this module
    /// (`UsageState::admit`). Not compiled into the gateway.
    #[cfg(any(test, feature = "db-tests"))]
    pub fn for_test(hold_id: &str) -> Self {
        Self(hold_id.to_string())
    }
}

/// What an admission reserved.
#[derive(Debug)]
pub struct Admitted {
    /// The output tokens the call may produce (the squeeze of its limits).
    pub out_tokens: i64,
    /// The hold it reserved under, when it was asked to hold.
    pub hold: Option<Held>,
}

/// A hold a gate asks an admission for, and what came of it: handed to the
/// functions that admit on a caller's behalf, so the caller adopts a guard
/// only for an admission that happened.
#[derive(Debug)]
pub struct PendingHold {
    id: HoldId,
    held: Option<Held>,
}

impl PendingHold {
    pub fn of(id: HoldId) -> Self {
        Self { id, held: None }
    }

    pub fn id(&self) -> &HoldId {
        &self.id
    }

    /// Record what the admission returned.
    pub fn admitted_as(&mut self, admitted: Admitted) {
        self.held = admitted.hold;
    }

    /// The hold, if the admission reserved under it.
    pub fn held(self) -> Option<Held> {
        self.held
    }
}

/// One dimension and its refusal.
#[derive(Debug, Clone)]
pub struct LimitDim {
    pub dim: Dim,
    pub refusal: DimRefusal,
}

impl LimitDim {
    /// Whether this dimension can squeeze a call's output: a LIMITED counter
    /// that grows with the output. When any does, the admitted output must
    /// be enforced upstream as the call's output ceiling, or the call could
    /// spend past what it reserved.
    pub fn squeezes_output(&self) -> bool {
        use crate::services::cache::usage_state::DimKind;
        matches!(self.dim.kind, DimKind::Counter { limit: Some(_), .. })
            && matches!(
                self.dim.measure,
                Measure::OutTokens
                    | Measure::TotalTokens
                    | Measure::CostMicros
                    | Measure::CostCents
            )
    }
}

/// The unit a cumulative limit counts in (for the refusal's figures).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Unit {
    Count,
    Tokens,
    /// Micro-USD in the counter, shown in USD.
    Micros,
}

impl Unit {
    fn show(self, v: f64) -> String {
        match self {
            Unit::Micros => format!("{:.4}", v / 1_000_000.0),
            _ => format!("{}", v.round() as i64),
        }
    }

    pub fn of(m: Measure) -> Self {
        match m {
            Measure::One => Unit::Count,
            Measure::CostMicros | Measure::CostCents => Unit::Micros,
            _ => Unit::Tokens,
        }
    }
}

/// How a rate refusal tells the caller when to retry.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum RateRetry {
    /// A penalty box: retry when it lapses.
    Cooldown,
    /// A bucket: retry when it refills (or after the scope's cooldown, if
    /// one is configured — a tripped bucket arms it).
    Bucket { cooldown_secs: Option<i64> },
    /// A fixed window: retry when it rolls over. Carries the limit for the
    /// message.
    Window { window: QuotaWindow, limit: i64 },
    /// A fixed window of a configured length (epoch-aligned, like the
    /// others): retry when it rolls over.
    Every { secs: u64, limit: i64 },
}

#[derive(Debug, Clone, PartialEq)]
pub enum DimRefusal {
    /// A rate limit (frequency or throughput): 429 with Retry-After.
    Rate {
        scope: &'static str,
        limit_type: &'static str,
        what: String,
        retry: RateRetry,
    },
    /// A cumulative quota or budget of a group, the tenant, or an ancestor.
    Quota {
        owner_id: String,
        owner_name: String,
        limit_type: String,
        limit: i64,
        unit: Unit,
    },
    /// A run cap: `term` as `run_budget` names it.
    Run {
        term: &'static str,
        limit: i64,
        unit: Unit,
    },
    /// A conversation cap.
    Session { term: &'static str, limit: i64 },
    /// An accounting-only dimension: it never refuses.
    Accounting,
}

/// A refused admission: which limit, and the state it refused at.
#[derive(Debug, Clone, PartialEq)]
pub struct Refused {
    pub refusal: DimRefusal,
    pub current: f64,
    pub retry_after_ms: u64,
}

impl Refused {
    /// The rate-limit block for a `Rate` refusal.
    pub fn rate_block(&self) -> Option<McpRateLimitBlock> {
        let DimRefusal::Rate {
            scope,
            limit_type,
            what,
            retry,
        } = &self.refusal
        else {
            return None;
        };
        let (message, retry_after_secs) = match retry {
            RateRetry::Cooldown => {
                let secs = (self.current / 1000.0).ceil().max(1.0) as u64;
                (format!("{what} — retry in {secs}s"), secs)
            }
            RateRetry::Bucket { cooldown_secs } => {
                let secs = cooldown_secs
                    .map(|c| c as u64)
                    .unwrap_or_else(|| self.retry_after_ms.div_ceil(1000))
                    .max(1);
                (format!("{what} — retry in {secs}s"), secs)
            }
            RateRetry::Window { window, limit } => (
                format!("{what} ({}/{limit})", self.current.round() as i64),
                secs_until_window_reset(*window),
            ),
            RateRetry::Every { secs, limit } => (
                format!("{what} ({}/{limit})", self.current.round() as i64),
                secs_until_reset(*secs),
            ),
        };
        Some(McpRateLimitBlock {
            scope,
            limit_type,
            message,
            retry_after_secs,
        })
    }

    /// The 429 usage-limit result for a quota, control, run or session
    /// refusal (and a rate refusal, for callers that only speak quota).
    pub fn quota_result(&self) -> UsageEnforcementResult {
        let blocked =
            |owner_id: &str, owner_name: &str, lt: &str, cur: String, max: String, msg: String| {
                UsageEnforcementResult {
                    blocked: true,
                    group_id: Some(owner_id.to_string()),
                    group_name: Some(owner_name.to_string()),
                    message: Some(msg),
                    limit_type: Some(lt.to_string()),
                    current_usage: Some(cur),
                    max_limit: Some(max),
                    warning_headers: Vec::new(),
                }
            };
        match &self.refusal {
            DimRefusal::Quota {
                owner_id,
                owner_name,
                limit_type,
                limit,
                unit,
            } => {
                let (cur, max) = (unit.show(self.current), unit.show(*limit as f64));
                blocked(
                    owner_id,
                    owner_name,
                    limit_type,
                    cur.clone(),
                    max.clone(),
                    format!(
                        "{limit_type} reached: {cur} used or reserved of {max} — this request's worst case does not fit what is left"
                    ),
                )
            }
            DimRefusal::Run { term, limit, unit } => {
                let (cur, max) = (unit.show(self.current), unit.show(*limit as f64));
                let mut r = blocked(
                    "",
                    "",
                    term,
                    cur.clone(),
                    max.clone(),
                    format!("Run limit reached: {term} {cur}/{max}"),
                );
                r.group_id = None;
                r.group_name = None;
                r
            }
            DimRefusal::Session { term, limit } => {
                let (cur, max) = (
                    Unit::Micros.show(self.current),
                    Unit::Micros.show(*limit as f64),
                );
                let mut r = blocked(
                    "",
                    "",
                    term,
                    cur.clone(),
                    max.clone(),
                    format!("Conversation cost ceiling reached: ${cur} of ${max}"),
                );
                r.group_id = None;
                r.group_name = None;
                r
            }
            DimRefusal::Rate { .. } => {
                let b = self.rate_block().expect("rate");
                let mut r = blocked(
                    "",
                    "",
                    b.limit_type,
                    String::new(),
                    String::new(),
                    b.message,
                );
                r.group_id = None;
                r.group_name = None;
                r
            }
            DimRefusal::Accounting => blocked(
                "",
                "",
                "accounting",
                String::new(),
                String::new(),
                "an accounting dimension refused (it has no limit)".to_string(),
            ),
        }
    }
}

/// Admit `dims` in one step. `Ok(Ok(admitted))` = admitted (every
/// dimension reserved; `admitted.hold` is the hold it reserved under, when
/// `hold_id` named one); `Ok(Err(refused))` = refused (nothing reserved, and
/// so no hold); `Err` = the counters cannot be reached, and the caller
/// refuses (fail closed).
pub async fn admit(
    us: &UsageState,
    hold_id: Option<&HoldId>,
    dims: Vec<LimitDim>,
    shape: Shape,
) -> Result<Result<Admitted, Refused>, redis::RedisError> {
    // A scope's penalty box is named by both its frequency and its throughput
    // dimensions; one check of it is enough (two would be one key twice).
    let mut cooldowns = std::collections::HashSet::new();
    let (dims, refusals): (Vec<Dim>, Vec<DimRefusal>) = dims
        .into_iter()
        .filter(|d| match &d.dim.kind {
            crate::services::cache::usage_state::DimKind::Cooldown { key } => {
                cooldowns.insert(key.clone())
            }
            _ => true,
        })
        .map(|d| (d.dim, d.refusal))
        .unzip();
    let adm = Admission {
        hold_id: hold_id.map(|h| h.as_str().to_string()),
        dims,
        shape,
    };
    Ok(match us.admit(&adm).await? {
        Verdict::Admitted { out_tokens } => Ok(Admitted {
            out_tokens,
            hold: hold_id.map(|h| Held(h.as_str().to_string())),
        }),
        Verdict::Refused {
            dim,
            current,
            retry_after_ms,
        } => Err(Refused {
            refusal: refusals.get(dim).cloned().unwrap_or(DimRefusal::Accounting),
            current,
            retry_after_ms,
        }),
    })
}

/// The block for a request refused because its limits could not be checked
/// (the counter store is unreachable). Fail closed: a limit nobody can read
/// is not a limit nobody set.
pub fn unavailable_block(e: &redis::RedisError) -> McpRateLimitBlock {
    tracing::error!(error = %e, "limit counters unreachable; refusing (fail closed)");
    McpRateLimitBlock {
        scope: "limits",
        limit_type: "limits_unavailable",
        message: "Refused: the usage limits for this request cannot be checked right now".into(),
        retry_after_secs: 5,
    }
}

/// [`unavailable_block`] as a usage-limit result.
pub fn unavailable_result(e: &redis::RedisError) -> UsageEnforcementResult {
    let b = unavailable_block(e);
    UsageEnforcementResult {
        blocked: true,
        group_id: None,
        group_name: None,
        message: Some(b.message),
        limit_type: Some(b.limit_type.to_string()),
        current_usage: None,
        max_limit: None,
        warning_headers: Vec::new(),
    }
}

/// The block for a request refused because its limit CONFIGURATION could
/// not be read. Fail closed, like [`unavailable_block`].
pub fn config_unavailable_block(e: &sqlx::Error) -> McpRateLimitBlock {
    tracing::error!(error = %e, "limit configuration unreadable; refusing (fail closed)");
    McpRateLimitBlock {
        scope: "limits",
        limit_type: "limits_unavailable",
        message: "Refused: the usage limits for this request cannot be read right now".into(),
        retry_after_secs: 5,
    }
}

/// [`config_unavailable_block`] as a usage-limit result.
pub fn config_unavailable_result(e: &sqlx::Error) -> UsageEnforcementResult {
    let b = config_unavailable_block(e);
    UsageEnforcementResult {
        blocked: true,
        group_id: None,
        group_name: None,
        message: Some(b.message),
        limit_type: Some(b.limit_type.to_string()),
        current_usage: None,
        max_limit: None,
        warning_headers: Vec::new(),
    }
}

// ---------------------------------------------------------------------------
// Frequency: cooldown → bucket (per minute + burst) → hourly → daily
// ---------------------------------------------------------------------------

/// One `frequency` block (MCP / LLM / A2A share the shape).
#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct FreqLimits {
    pub per_minute: Option<i64>,
    pub per_hour: Option<i64>,
    pub per_day: Option<i64>,
    pub burst: Option<i64>,
    pub cooldown_secs: Option<i64>,
}

impl FreqLimits {
    /// True when no rate or quota field is set — nothing to enforce.
    /// `burst` / `cooldown_secs` are inert without a rate, so they don't count.
    pub fn is_empty(&self) -> bool {
        self.per_minute.is_none() && self.per_hour.is_none() && self.per_day.is_none()
    }

    /// Parse the five frequency fields out of a JSON object. Non-positive
    /// or non-integer values are treated as unset.
    pub fn parse(v: &serde_json::Value) -> Self {
        let get = |k: &str| v.get(k).and_then(|x| x.as_i64()).filter(|n| *n > 0);
        Self {
            per_minute: get("max_calls_per_minute"),
            per_hour: get("max_calls_per_hour"),
            per_day: get("max_calls_per_day"),
            burst: get("burst_limit"),
            cooldown_secs: get("cooldown_seconds"),
        }
    }

    /// Navigate `path` into an optional limits blob and parse what is there.
    pub fn at(limits: Option<&serde_json::Value>, path: &[&str]) -> Self {
        let mut node = match limits {
            Some(v) => v,
            None => return Self::default(),
        };
        for seg in path {
            node = match node.get(*seg) {
                Some(n) => n,
                None => return Self::default(),
            };
        }
        Self::parse(node)
    }
}

/// The cooldown (penalty box) key of a rate scope.
pub fn cooldown_key(scope_key: &str) -> String {
    format!("brutor:mcp:cd:{scope_key}")
}

/// The fixed-window counter key of a rate scope: epoch-aligned windows (the
/// scheme the per-call quotas have always used, so counters carry over).
pub fn freq_window_key(scope_key: &str, window: QuotaWindow) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    format!(
        "brutor:mcp:q:{}:{}:{}",
        window.label(),
        scope_key,
        now / window.secs()
    )
}

/// Seconds until the current fixed quota window rolls over (epoch-aligned).
pub fn secs_until_window_reset(window: QuotaWindow) -> u64 {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let w = window.secs();
    w - (now % w)
}

/// Seconds until the current fixed window of `secs` seconds rolls over.
fn secs_until_reset(secs: u64) -> u64 {
    let secs = secs.max(1);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    secs - (now % secs)
}

/// The scope and limit type of the limit on new runs, as its refusal and
/// its audit row name them (`rate_limit_credential`).
pub const NEW_RUNS_SCOPE: &str = "credential";
pub const NEW_RUNS_LIMIT_TYPE: &str = "new_runs";

/// The key of the set of runs `principal` started in the current window of
/// `window_secs` seconds (epoch-aligned, the scheme of every fixed window
/// here).
pub fn new_runs_key(tenant_id: &str, principal: &str, window_secs: u64) -> String {
    let window_secs = window_secs.max(1);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    format!(
        "brutor:runs:{tenant_id}:{principal}:{window_secs}:{}",
        now / window_secs
    )
}

/// The limit on distinct runs one credential starts in a window: the set of
/// run roots `principal` has started in the current window, at most `max`.
/// A run already in the set continues and charges nothing; "is this run new"
/// is decided by the admission that adds it (`DimKind::Members`). `term`
/// names the setting in the refusal.
pub fn new_runs_dim(
    tenant_id: &str,
    principal: &str,
    run_id: &str,
    max: i64,
    window_secs: u64,
    term: &str,
) -> LimitDim {
    let window_secs = window_secs.max(1);
    LimitDim {
        dim: Dim::members(
            new_runs_key(tenant_id, principal, window_secs),
            run_id.to_string(),
            max,
            window_secs * 2,
        ),
        refusal: DimRefusal::Rate {
            scope: NEW_RUNS_SCOPE,
            limit_type: NEW_RUNS_LIMIT_TYPE,
            what: format!(
                "Too many new runs started by this credential in {window_secs}s ({term})"
            ),
            retry: RateRetry::Every {
                secs: window_secs,
                limit: max,
            },
        },
    }
}

/// The dimensions of one frequency scope: penalty box, sustained rate +
/// burst, hourly and daily quota. `product` names the surface in messages
/// ("MCP", "LLM", "A2A").
pub fn freq_dims(
    product: &str,
    scope: &'static str,
    scope_key: &str,
    f: &FreqLimits,
) -> Vec<LimitDim> {
    if f.is_empty() {
        return Vec::new();
    }
    let cd = cooldown_key(scope_key);
    let mut out = vec![LimitDim {
        dim: Dim::cooldown(cd.clone()),
        refusal: DimRefusal::Rate {
            scope,
            limit_type: "cooldown",
            what: format!("{product} rate-limit cooldown active at {scope} scope"),
            retry: RateRetry::Cooldown,
        },
    }];
    if let Some(per_min) = f.per_minute {
        let capacity = f.burst.unwrap_or(per_min).max(1) as f64;
        out.push(LimitDim {
            dim: Dim::bucket(
                scope_key.to_string(),
                capacity,
                per_min as f64 / 60.0,
                Measure::One,
            )
            .arming(f.cooldown_secs.map(|c| (cd.clone(), c as u64))),
            refusal: DimRefusal::Rate {
                scope,
                limit_type: "rate",
                what: format!("{product} rate limit exceeded at {scope} scope ({per_min}/min)"),
                retry: RateRetry::Bucket {
                    cooldown_secs: f.cooldown_secs,
                },
            },
        });
    }
    for (limit, window, limit_type, label) in [
        (f.per_hour, QuotaWindow::Hour, "hourly_quota", "hourly"),
        (f.per_day, QuotaWindow::Day, "daily_quota", "daily"),
    ] {
        if let Some(limit) = limit {
            out.push(LimitDim {
                dim: Dim::counter(
                    freq_window_key(scope_key, window),
                    Some(limit),
                    window.secs() * 2,
                    Measure::One,
                ),
                refusal: DimRefusal::Rate {
                    scope,
                    limit_type,
                    what: format!("{product} {label} quota exceeded at {scope} scope"),
                    retry: RateRetry::Window { window, limit },
                },
            });
        }
    }
    out
}

/// An absolute per-window count cap (A2A `invocations`, agent-grant rates):
/// one counter on the epoch-aligned window.
pub fn window_count_dim(
    scope: &'static str,
    scope_key: &str,
    window: QuotaWindow,
    limit: i64,
    limit_type: &'static str,
    what: String,
) -> LimitDim {
    LimitDim {
        dim: Dim::counter(
            freq_window_key(scope_key, window),
            Some(limit),
            window.secs() * 2,
            Measure::One,
        ),
        refusal: DimRefusal::Rate {
            scope,
            limit_type,
            what,
            retry: RateRetry::Window { window, limit },
        },
    }
}

// ---------------------------------------------------------------------------
// Cumulative quotas (budget / tokens / requests; execution counts)
// ---------------------------------------------------------------------------

/// Who owns a cumulative limit, for its refusal.
#[derive(Debug, Clone, PartialEq)]
pub struct Owner {
    pub id: String,
    pub name: String,
}

impl Owner {
    pub fn new(id: &str, name: &str) -> Self {
        Self {
            id: id.to_string(),
            name: name.to_string(),
        }
    }
}

/// A day or month cumulative counter on the usage-counter keyspace
/// (`brutor:uq:…`), the same key every reader and the reconcile heal use.
pub fn usage_dim(
    counter_key: &str,
    window: QuotaWindow,
    limit: Option<i64>,
    measure: Measure,
    refusal: DimRefusal,
) -> LimitDim {
    LimitDim {
        dim: Dim::counter(
            UsageState::usage_key(counter_key, window),
            limit,
            window.ttl_secs(),
            measure,
        ),
        refusal: if limit.is_some() {
            refusal
        } else {
            DimRefusal::Accounting
        },
    }
}

/// The `budget` / `tokens` / `requests` day+month dimensions of one scope
/// (`grp:{g}`, `grp:{g}:mdl:{m}`, `ten:{t}:mdl:{m}`, `sub:{a}`). A dimension
/// with no configured limit is still reserved and settled when `account`
/// (the group's own counters, which the dashboards and the reconcile read),
/// and skipped otherwise. `names` maps (family, daily) to the refusal's
/// `limit_type`; the families are `cost`, `tok`, `req` and, for the input
/// and output side of a token quota, `tokin` and `tokout`.
pub fn cumulative_dims(
    scope: &str,
    limits: Option<&serde_json::Value>,
    owner: &Owner,
    account: bool,
    names: impl Fn(&str, bool) -> String,
) -> Vec<LimitDim> {
    let f = |b: &str, k: &str| {
        limits
            .and_then(|l| l.get(b))
            .and_then(|v| v.get(k))
            .and_then(|x| x.as_f64())
            .filter(|n| *n > 0.0)
    };
    let mut out = Vec::new();
    for (family, block, measure) in [
        ("cost", "budget", Measure::CostMicros),
        ("tok", "tokens", Measure::TotalTokens),
        ("req", "requests", Measure::One),
    ] {
        for (window, field) in [(QuotaWindow::Day, "daily"), (QuotaWindow::Month, "monthly")] {
            let limit = if family == "cost" {
                f(block, &format!("{field}_limit_usd"))
                    .map(|usd| (usd * 1_000_000.0).round() as i64)
            } else {
                f(block, &format!("{field}_limit")).map(|n| n as i64)
            };
            if limit.is_none() && !account {
                continue;
            }
            let daily = window == QuotaWindow::Day;
            out.push(usage_dim(
                &format!("{family}:{scope}"),
                window,
                limit,
                measure,
                DimRefusal::Quota {
                    owner_id: owner.id.clone(),
                    owner_name: owner.name.clone(),
                    limit_type: names(family, daily),
                    limit: limit.unwrap_or(0),
                    unit: Unit::of(measure),
                },
            ));
        }
    }
    // The input and the output side of the token quota
    // (`tokens.{daily,monthly}_{input,output}_limit`): counters of their
    // own, present only where a limit is stated (the totals above are what
    // the dashboards read). Healed from the ledger like the totals
    // (`usage::reconcile_llm_shadow_counters`).
    for (family, side, measure) in [
        ("tokin", "input", Measure::InTokens),
        ("tokout", "output", Measure::OutTokens),
    ] {
        for (window, field) in [(QuotaWindow::Day, "daily"), (QuotaWindow::Month, "monthly")] {
            let Some(limit) = f("tokens", &format!("{field}_{side}_limit")).map(|n| n as i64)
            else {
                continue;
            };
            out.push(usage_dim(
                &format!("{family}:{scope}"),
                window,
                Some(limit),
                measure,
                DimRefusal::Quota {
                    owner_id: owner.id.clone(),
                    owner_name: owner.name.clone(),
                    limit_type: names(family, window == QuotaWindow::Day),
                    limit,
                    unit: Unit::of(measure),
                },
            ));
        }
    }
    out
}

/// The `execution.daily_limit` / `monthly_limit` count dimensions of one
/// skill scope. `names` = (daily, monthly) refusal names.
pub fn execution_dims(
    scope: &str,
    limits: Option<&serde_json::Value>,
    owner: &Owner,
    account: bool,
    names: (&str, &str),
) -> Vec<LimitDim> {
    let exec = limits.and_then(|l| l.get("execution"));
    let mut out = Vec::new();
    for (window, field, name) in [
        (QuotaWindow::Day, "daily_limit", names.0),
        (QuotaWindow::Month, "monthly_limit", names.1),
    ] {
        let limit = exec
            .and_then(|e| e.get(field))
            .and_then(|v| v.as_i64())
            .filter(|l| *l > 0);
        if limit.is_none() && !account {
            continue;
        }
        out.push(usage_dim(
            &format!("skx:{scope}"),
            window,
            limit,
            Measure::One,
            DimRefusal::Quota {
                owner_id: owner.id.clone(),
                owner_name: owner.name.clone(),
                limit_type: name.to_string(),
                limit: limit.unwrap_or(0),
                unit: Unit::Count,
            },
        ));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_hold_id_names_its_tenant_and_its_surface_and_is_its_requests_own() {
        let a = HoldId::new("tenant-a", "llm");
        let b = HoldId::new("tenant-a", "llm");
        assert_ne!(a, b, "two admissions never share a hold");
        assert!(a.as_str().starts_with("tenant-a:llm:"), "{a}");
        let own = a.as_str().rsplit(':').next().unwrap();
        assert_eq!(own.len(), 32, "a part generated per admission: {a}");
        assert!(
            HoldId::new("tenant-b", "llm")
                .as_str()
                .starts_with("tenant-b:"),
            "another tenant's hold is named for that tenant"
        );
    }

    #[test]
    fn the_input_and_output_token_quotas_are_dimensions_only_where_stated() {
        use crate::services::cache::usage_state::DimKind;
        let owner = Owner::new("g", "G");
        let names = |f: &str, d: bool| format!("{f}-{d}");
        // No side stated: nothing beyond the six accounted totals.
        let totals = json!({"tokens": {"daily_limit": 1000}});
        assert_eq!(
            cumulative_dims("grp:g", Some(&totals), &owner, true, names).len(),
            6
        );
        let limits = json!({"tokens": {
            "daily_input_limit": 800, "monthly_input_limit": 16000,
            "daily_output_limit": 200, "monthly_output_limit": 0
        }});
        let dims = cumulative_dims("grp:g", Some(&limits), &owner, false, names);
        let got: Vec<(String, Measure, Option<i64>, String)> = dims
            .iter()
            .map(|d| {
                let DimKind::Counter { key, limit, .. } = &d.dim.kind else {
                    panic!("a quota is a counter");
                };
                let DimRefusal::Quota { limit_type, .. } = &d.refusal else {
                    panic!("a stated quota refuses");
                };
                // The key without its window index.
                let family = key.split("tok").nth(1).unwrap_or_default();
                let family = format!("tok{}", family.split(':').next().unwrap_or_default());
                (family, d.dim.measure, *limit, limit_type.clone())
            })
            .collect();
        assert_eq!(
            got,
            [
                (
                    "tokin".to_string(),
                    Measure::InTokens,
                    Some(800),
                    "tokin-true".to_string()
                ),
                (
                    "tokin".to_string(),
                    Measure::InTokens,
                    Some(16000),
                    "tokin-false".to_string()
                ),
                // A limit of zero is no limit, as for every cumulative quota.
                (
                    "tokout".to_string(),
                    Measure::OutTokens,
                    Some(200),
                    "tokout-true".to_string()
                ),
            ]
        );
        // An output quota squeezes the call's output, as a total does.
        assert!(dims[2].squeezes_output());
        assert!(!dims[0].squeezes_output());
    }

    #[test]
    fn an_unset_cumulative_limit_is_accounted_only_when_asked() {
        let owner = Owner::new("g", "G");
        let limits = json!({"budget": {"daily_limit_usd": 2.5}});
        let names = |f: &str, d: bool| format!("{f}-{d}");
        let capped = cumulative_dims("grp:g", Some(&limits), &owner, false, names);
        assert_eq!(capped.len(), 1, "only the configured dimension");
        assert!(matches!(
            capped[0].dim.kind,
            crate::services::cache::usage_state::DimKind::Counter {
                limit: Some(2_500_000),
                ..
            }
        ));
        let all = cumulative_dims("grp:g", Some(&limits), &owner, true, names);
        assert_eq!(all.len(), 6, "cost/tok/req × day/month");
        assert_eq!(
            all.iter()
                .filter(|d| d.refusal == DimRefusal::Accounting)
                .count(),
            5
        );
    }

    #[test]
    fn frequency_builds_cooldown_bucket_and_windows_in_order() {
        let f = FreqLimits::parse(&json!({
            "max_calls_per_minute": 60, "max_calls_per_hour": 100,
            "max_calls_per_day": 1000, "cooldown_seconds": 30
        }));
        let d = freq_dims("MCP", "group", "g:x", &f);
        let types: Vec<_> = d
            .iter()
            .map(|d| match &d.refusal {
                DimRefusal::Rate { limit_type, .. } => *limit_type,
                _ => "?",
            })
            .collect();
        assert_eq!(types, ["cooldown", "rate", "hourly_quota", "daily_quota"]);
        assert_eq!(
            d[1].dim.cooldown_on_refuse,
            Some(("brutor:mcp:cd:g:x".to_string(), 30))
        );
    }

    #[test]
    fn a_rate_refusal_reads_like_the_old_limiter() {
        let r = Refused {
            refusal: DimRefusal::Rate {
                scope: "group",
                limit_type: "hourly_quota",
                what: "MCP hourly quota exceeded at group scope".into(),
                retry: RateRetry::Window {
                    window: QuotaWindow::Hour,
                    limit: 5,
                },
            },
            current: 5.0,
            retry_after_ms: 0,
        };
        let b = r.rate_block().unwrap();
        assert_eq!(b.message, "MCP hourly quota exceeded at group scope (5/5)");
        assert!(b.retry_after_secs >= 1 && b.retry_after_secs <= 3600);
    }
}

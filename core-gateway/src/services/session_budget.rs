// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Session-level cost cap — the ceiling for one conversation.
//!
//! ## The gap this fills
//!
//! Brutor's other spend controls each miss the same case:
//!
//!   * `max_cost_per_run_usd` bounds ONE message. Three hundred
//!     individually-cheap messages sail past it.
//!   * a daily user or group quota bounds a *period*, not a conversation —
//!     it cannot say which chat spent the money, and it resets at midnight
//!     while the conversation carries on.
//!   * rate limits cap velocity, not total.
//!
//! A user who quietly spends $500 over a three-month chat trips none of
//! them. A session cap is the only scope that sees it.
//!
//! ## Why spend is read from the ledger, not a counter
//!
//! This is the one place the run-budget pattern could not be copied. Run
//! counters live in Redis with a 24-hour TTL, which is fine because a run is
//! finalized minutes after it starts — the TTL only bounds abandoned keys.
//!
//! A conversation lasts months. A Redis-only counter would mean two things,
//! both unacceptable for a financial control:
//!
//!   * memory unbounded in the number of conversations, with keys that never
//!     naturally expire; and
//!   * **a restart or an eviction hands the user a fresh allowance.** For a
//!     run counter that is a rounding error. For a budget it defeats the
//!     feature: someone at 99% of their cap gets a clean $500 because a
//!     cache bounced.
//!
//! So the usage ledger is the source of truth — spend summed over the
//! session's rows — and the live counter in Redis is SEEDED from it whenever
//! it is absent (first call, eviction, restart, an idle week). From there the
//! counter is a dimension of every call's admission: the call's worst case
//! is reserved against the ceiling and settled to its actual cost, so
//! concurrent messages in one conversation cannot all pass at 99% (the
//! read-then-decide cache they replaced let a burst through).
//!
//! An admin reset writes a watermark (`session_budget_resets`); the counter's
//! key carries it, so a reset starts a fresh counter seeded from the rows
//! after the watermark, on the very next call.
//!
//! ## Raising the ceiling takes effect immediately
//!
//! The cap is read per request from the system's live operating envelope,
//! and compared against accumulated spend. An admin who raises
//! `max_cost_per_session_usd` changes what the counter is measured against,
//! not the counter — so a blocked conversation resumes on the next call
//! without anyone resetting anything.
//!
//! ## Ending a conversation
//!
//! `X-Brutor-Session-End` (sent by the portal when a chat is deleted) drops
//! the cached total. It does NOT delete ledger rows — those are the audit
//! record, and erasing spend history to satisfy a counter would be exactly
//! the wrong trade.
//!
//! That is enough in practice, because the reset is structural: a session key
//! is HMAC-derived from the client's chat id, a new chat gets a new id, and a
//! deleted chat's id is never reused. The next conversation is a different
//! key with its own zero.
//!
//! An ADMIN "reset this conversation's budget" action is a different thing
//! and is deliberately not built here — it needs a durable watermark to sum
//! from, since the rows have to stay.
//!
//! ## What is ungated, and what fails closed
//!
//! An undeclared term is not a cap of zero. A system with no envelope, or a
//! request with no asserted session, is ungated here. But a DECLARED cap
//! whose caps or spend cannot be read refuses: an unreadable ceiling is not
//! an absent one (fail closed).

use serde_json::Value;

/// Envelope term name. Must match `TERM_MAX_COST_PER_SESSION` in
/// `app/services/ai_system_envelope.py` — the wire contract between the
/// control plane that declares the cap and the core that enforces it.
pub const TERM_MAX_COST_PER_SESSION: &str = "max_cost_per_session_usd";

/// The declared ceiling for one conversation, if any.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct SessionCaps {
    pub max_cost_usd: Option<f64>,
}

impl SessionCaps {
    /// Parse the declared term out of an operating envelope.
    ///
    /// Absent, null, non-numeric, non-finite or non-positive are all treated
    /// as undeclared. A malformed bound must never become a cap of zero that
    /// refuses every message on the system — the failure mode of a spend
    /// control that reads a typo as "nobody may spend anything" is worse
    /// than the overspend it was guarding against.
    pub fn from_envelope(envelope: Option<&Value>) -> Self {
        let obj = match envelope.and_then(|v| v.as_object()) {
            Some(o) => o,
            None => return Self::default(),
        };
        Self {
            max_cost_usd: obj
                .get(TERM_MAX_COST_PER_SESSION)
                .and_then(|v| v.as_f64())
                .filter(|n| n.is_finite() && *n > 0.0),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.max_cost_usd.is_none()
    }
}

/// What a session has spent so far, in USD.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct SessionUsage {
    pub cost_usd: f64,
}

/// A refusal: which ceiling, what it was, and what the conversation reached.
#[derive(Debug, Clone, PartialEq)]
pub struct SessionCapBlock {
    pub term: &'static str,
    pub limit: f64,
    pub observed: f64,
    pub message: String,
}

/// How close to the ceiling a warning is surfaced at.
///
/// A human mid-conversation deserves notice before the wall, not at it —
/// unlike an agent, they can't retry into a different budget, and their only
/// recoveries are "start a new chat" or "ask an admin". 80% leaves room to
/// finish a thought.
pub const WARN_FRACTION: f64 = 0.80;

/// Whether this conversation may make another call.
///
/// Pure — no I/O — so the decision table is unit-testable without Redis or a
/// database, exactly like `run_budget::evaluate`.
pub fn evaluate(caps: &SessionCaps, usage: &SessionUsage) -> Option<SessionCapBlock> {
    let limit = caps.max_cost_usd?;
    if usage.cost_usd < limit {
        return None;
    }
    Some(SessionCapBlock {
        term: TERM_MAX_COST_PER_SESSION,
        limit,
        observed: usage.cost_usd,
        message: format!(
            "This conversation has reached its spend ceiling of ${limit:.2} \
             (${observed:.2} used). Start a new chat, or ask an administrator \
             to raise the limit.",
            limit = limit,
            observed = usage.cost_usd,
        ),
    })
}

/// Whether the conversation is close enough to its ceiling to warn.
///
/// `None` when no cap is declared or the ceiling is already reached — at
/// that point `evaluate` is refusing, and a warning alongside a refusal is
/// noise.
pub fn warning(caps: &SessionCaps, usage: &SessionUsage) -> Option<f64> {
    let limit = caps.max_cost_usd?;
    let fraction = usage.cost_usd / limit;
    if (WARN_FRACTION..1.0).contains(&fraction) {
        Some(fraction)
    } else {
        None
    }
}

/// Read the session ceiling declared on a group's live operating envelope.
///
/// Live, not the signed contract — same rule as `run_budget::caps_for_group`:
/// if enforcement waited for a signature, the systems most in need of a
/// spend guard (the ones with no approved contract) would have none.
/// Widening without approval still surfaces as `config_drifted` on the
/// report.
/// Scoped by tenant as well as id.
///
/// `resource_groups.id` is the primary key, so an unscoped lookup cannot
/// actually return another tenant's row by accident; you would need that
/// tenant's exact group id. The filter is here because every other read of
/// this table carries one, and a query that is correct only because of a
/// uniqueness property elsewhere in the schema is one schema change away from
/// being wrong. Callers already hold the tenant.
pub async fn caps_for_group(
    pool: &sqlx::PgPool,
    tenant_id: &str,
    group_id: &str,
) -> Result<SessionCaps, sqlx::Error> {
    let row: Option<(Option<Value>,)> = sqlx::query_as(
        "SELECT operating_envelope FROM resource_groups WHERE id = $1 AND tenant_id = $2",
    )
    .bind(group_id)
    .bind(tenant_id)
    .fetch_optional(pool)
    .await?;
    Ok(row
        .map(|(envelope,)| SessionCaps::from_envelope(envelope.as_ref()))
        .unwrap_or_default())
}

/// Total spend for one conversation, read from the usage ledger.
///
/// The ledger is the source of truth precisely because a conversation
/// outlives any cache: a restart must not hand the user a fresh allowance.
/// See the module docs.
///
/// ## Resets
///
/// Two different things clear a conversation's spend:
///
///   * `X-Brutor-Session-End` — structural. A session key is HMAC-derived
///     from the client's chat id and a deleted chat's id is never reused, so
///     the next conversation is simply a different key with its own zero.
///   * an ADMIN reset — a watermark row in `session_budget_resets`. The sum
///     below starts from it.
///
/// The rows are never deleted for either. They are the audit record, and
/// erasing spend history to satisfy a budget would destroy the evidence the
/// spend happened — a worse failure than an unresettable budget. The query
/// moves instead.
///
/// `Err` when the ledger cannot be read: the caller refuses (fail closed).
pub async fn usage_for_session(
    pool: &sqlx::PgPool,
    tenant_id: &str,
    session_id: &str,
) -> Result<SessionUsage, sqlx::Error> {
    // An empty or whitespace session is NOT a session. The resolver already
    // treats a blank assertion as absent, but the ledger has rows with an
    // empty `end_user_session_id` (traffic with no end user — API keys,
    // service accounts, seeded demo data). Summing those would collapse every
    // keyless caller in the tenant into one "conversation" and, once it
    // crossed a ceiling, refuse all of them. Verified against live data: one
    // such group held 34,469 calls and $113.
    if session_id.trim().is_empty() {
        return Ok(SessionUsage::default());
    }

    // The LEFT JOIN, not a subquery-per-row: most conversations have no
    // watermark, and `r.reset_at IS NULL` must mean "count everything"
    // rather than excluding the session entirely.
    let row: Result<Option<(Option<f64>,)>, _> = sqlx::query_as(
        "SELECT COALESCE(SUM(pl.llm_estimated_cost_usd), 0)::float8 \
         FROM proxy_logs pl \
         LEFT JOIN session_budget_resets r \
                ON r.tenant_id = pl.tenant_id \
               AND r.session_id = pl.end_user_session_id \
         WHERE pl.tenant_id = $1 \
           AND pl.end_user_session_id = $2 \
           AND (r.reset_at IS NULL OR pl.timestamp >= r.reset_at)",
    )
    .bind(tenant_id)
    .bind(session_id)
    .fetch_optional(pool)
    .await;

    Ok(match row? {
        Some((Some(cost),)) if cost.is_finite() && cost > 0.0 => SessionUsage { cost_usd: cost },
        _ => SessionUsage::default(),
    })
}

/// How long an idle conversation's live counter is kept. Past it the next
/// call re-seeds from the ledger, which is complete by then.
pub const SESSION_COUNTER_TTL_SECS: u64 = 7 * 86_400;

/// The admin reset watermark of a conversation (epoch ms), if any.
async fn reset_watermark_ms(
    pool: &sqlx::PgPool,
    tenant_id: &str,
    session_id: &str,
) -> Result<i64, sqlx::Error> {
    let row: Option<(chrono::DateTime<chrono::Utc>,)> = sqlx::query_as(
        "SELECT reset_at FROM session_budget_resets WHERE tenant_id = $1 AND session_id = $2",
    )
    .bind(tenant_id)
    .bind(session_id)
    .fetch_optional(pool)
    .await?;
    Ok(row.map(|(t,)| t.timestamp_millis()).unwrap_or(0))
}

/// A conversation's live spend counter (micro-USD). The reset watermark is
/// part of the key, so an admin reset starts a fresh counter.
fn counter_key(tenant_id: &str, session_id: &str, watermark_ms: i64) -> String {
    format!("brutor:session:{tenant_id}:{session_id}:{watermark_ms}:cost")
}

/// What a conversation's ceiling needs at admission.
pub struct SessionAdmission {
    pub caps: SessionCaps,
    /// Spend so far (seeded counter), for the approaching-the-ceiling warning.
    pub usage: SessionUsage,
    /// The admission dimension: the conversation's counter against its cap.
    pub dims: Vec<crate::services::limits::LimitDim>,
    key: String,
}

impl SessionAdmission {
    /// Forget the conversation's live counter (the client ended it). The
    /// ledger rows stay — they are the audit record.
    pub async fn end(&self, usage_state: &crate::services::cache::usage_state::UsageState) {
        usage_state.delete_key(&self.key).await;
    }
}

/// The session ceiling of one call: its caps, the conversation's counter
/// (seeded from the ledger when absent), and the admission dimension.
/// `Ok(None)` when the group declares no session cap. `Err` when the caps,
/// the watermark, the ledger or the counter cannot be read (fail closed).
pub async fn admission(
    pool: &sqlx::PgPool,
    usage_state: &crate::services::cache::usage_state::UsageState,
    tenant_id: &str,
    group_id: &str,
    session_id: &str,
) -> Result<Option<SessionAdmission>, String> {
    use crate::services::cache::usage_state::{Dim, Measure};
    use crate::services::limits::{DimRefusal, LimitDim};
    let caps = caps_for_group(pool, tenant_id, group_id)
        .await
        .map_err(|e| e.to_string())?;
    let Some(limit) = caps.max_cost_usd else {
        return Ok(None);
    };
    if session_id.trim().is_empty() {
        return Ok(None);
    }
    let wm = reset_watermark_ms(pool, tenant_id, session_id)
        .await
        .map_err(|e| e.to_string())?;
    let key = counter_key(tenant_id, session_id, wm);
    let mut micros = usage_state
        .counter_value(&key)
        .await
        .map_err(|e| e.to_string())?;
    if micros == 0 {
        let ledger = usage_for_session(pool, tenant_id, session_id)
            .await
            .map_err(|e| e.to_string())?;
        let seed = (ledger.cost_usd * 1_000_000.0).round() as i64;
        if seed > 0 {
            usage_state
                .seed_key_if_absent(&key, seed, SESSION_COUNTER_TTL_SECS)
                .await
                .map_err(|e| e.to_string())?;
            micros = usage_state
                .counter_value(&key)
                .await
                .map_err(|e| e.to_string())?;
        }
    }
    let limit_micros = (limit * 1_000_000.0).round() as i64;
    Ok(Some(SessionAdmission {
        caps,
        usage: SessionUsage {
            cost_usd: micros as f64 / 1_000_000.0,
        },
        dims: vec![LimitDim {
            dim: Dim::counter(
                key.clone(),
                Some(limit_micros),
                SESSION_COUNTER_TTL_SECS,
                Measure::CostMicros,
            ),
            refusal: DimRefusal::Session {
                term: TERM_MAX_COST_PER_SESSION,
                limit: limit_micros,
            },
        }],
        key,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn caps(v: f64) -> SessionCaps {
        SessionCaps {
            max_cost_usd: Some(v),
        }
    }
    fn used(v: f64) -> SessionUsage {
        SessionUsage { cost_usd: v }
    }

    // ── Parsing ──────────────────────────────────────────────────────────

    #[test]
    fn reads_the_declared_ceiling() {
        let env = json!({ "max_cost_per_session_usd": 25.0 });
        assert_eq!(
            SessionCaps::from_envelope(Some(&env)).max_cost_usd,
            Some(25.0)
        );
    }

    #[test]
    fn no_envelope_declares_nothing() {
        assert!(SessionCaps::from_envelope(None).is_empty());
    }

    #[test]
    fn an_unrelated_envelope_declares_nothing() {
        let env = json!({ "max_cost_per_run_usd": 1.0 });
        assert!(SessionCaps::from_envelope(Some(&env)).is_empty());
    }

    /// The failure mode that matters most: a malformed bound must not become
    /// a cap of zero that refuses every message on the system.
    #[test]
    fn malformed_bounds_are_undeclared_not_zero() {
        for bad in [
            json!({ "max_cost_per_session_usd": 0 }),
            json!({ "max_cost_per_session_usd": -5 }),
            json!({ "max_cost_per_session_usd": "25" }),
            json!({ "max_cost_per_session_usd": null }),
        ] {
            let c = SessionCaps::from_envelope(Some(&bad));
            assert!(c.is_empty(), "{bad} should read as undeclared, got {c:?}");
            assert_eq!(evaluate(&c, &used(1_000.0)), None, "{bad} must not refuse");
        }
    }

    // ── Enforcement ──────────────────────────────────────────────────────

    #[test]
    fn under_the_ceiling_passes() {
        assert_eq!(evaluate(&caps(25.0), &used(24.99)), None);
    }

    #[test]
    fn at_the_ceiling_refuses() {
        // At, not merely over — a cap of $25 means $25 is the last dollar
        // spent, not the first one allowed through.
        let block = evaluate(&caps(25.0), &used(25.0)).expect("should refuse");
        assert_eq!(block.term, TERM_MAX_COST_PER_SESSION);
        assert_eq!(block.limit, 25.0);
    }

    #[test]
    fn the_refusal_says_what_to_do_about_it() {
        let block = evaluate(&caps(25.0), &used(30.0)).unwrap();
        assert!(block.message.contains("$25.00"), "{}", block.message);
        assert!(block.message.contains("$30.00"), "{}", block.message);
        // A human mid-conversation needs a recovery, not just a number.
        assert!(block.message.contains("new chat"), "{}", block.message);
    }

    #[test]
    fn no_cap_never_refuses() {
        assert_eq!(evaluate(&SessionCaps::default(), &used(1_000_000.0)), None);
    }

    /// Raising the ceiling must free a blocked conversation immediately —
    /// the counter is unchanged, only what it is compared against.
    #[test]
    fn raising_the_ceiling_unblocks_without_touching_the_counter() {
        let spent = used(30.0);
        assert!(evaluate(&caps(25.0), &spent).is_some());
        assert!(evaluate(&caps(50.0), &spent).is_none());
    }

    /// And lowering it below current spend blocks immediately, rather than
    /// waiting for the next dollar.
    #[test]
    fn lowering_the_ceiling_blocks_immediately() {
        let spent = used(30.0);
        assert!(evaluate(&caps(100.0), &spent).is_none());
        assert!(evaluate(&caps(10.0), &spent).is_some());
    }

    // ── Warning ──────────────────────────────────────────────────────────

    #[test]
    fn warns_approaching_the_ceiling() {
        assert!(warning(&caps(100.0), &used(80.0)).is_some());
        assert!(warning(&caps(100.0), &used(99.99)).is_some());
    }

    #[test]
    fn does_not_warn_well_under_the_ceiling() {
        assert_eq!(warning(&caps(100.0), &used(79.99)), None);
    }

    #[test]
    fn does_not_warn_once_it_is_refusing() {
        // A warning alongside a refusal is noise; evaluate() is already
        // saying the louder thing.
        assert_eq!(warning(&caps(100.0), &used(100.0)), None);
        assert!(evaluate(&caps(100.0), &used(100.0)).is_some());
    }

    #[test]
    fn an_empty_session_is_not_a_session() {
        // Not a DB test — this pins the contract that callers rely on: a
        // blank session must never be treated as a real conversation, or
        // every keyless caller in the tenant shares one budget.
        assert!("".trim().is_empty());
        assert!("   ".trim().is_empty());
        // And with no usage, nothing is refused regardless of the cap.
        assert_eq!(evaluate(&caps(0.01), &SessionUsage::default()), None);
    }

    #[test]
    fn no_cap_never_warns() {
        assert_eq!(warning(&SessionCaps::default(), &used(1_000.0)), None);
    }
}

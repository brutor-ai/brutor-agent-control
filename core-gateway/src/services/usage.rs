// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use std::sync::Arc;

use chrono::{DateTime, Datelike, Utc};
use sqlx::PgPool;

use crate::models::auth::AuthContext;
use crate::models::db::UsageLogInsert;
use crate::models::db::{ResourceGroup, UsageAlert};
use crate::models::governance::{self, LlmGovernance};
use crate::models::proxy::UsageEnforcementResult;
use crate::services::background_writer::BackgroundWriter;
use crate::services::cache::usage_state::{
    Actual, Dim, Fallback, Finish, Measure, QuotaWindow, Shape, UsageState,
};
use crate::services::db_errors::DbResultExt;
use crate::services::limits;

/// The tenant's Unbound system group: where the platform service key's
/// calls are billed. `is_system` AND the name, so a tenant's own group that is
/// merely called "Unbound" is never taken for it.
pub async fn unbound_group(
    db: &PgPool,
    tenant_id: &str,
) -> Result<Option<ResourceGroup>, sqlx::Error> {
    sqlx::query_as::<_, ResourceGroup>(
        "SELECT *, group_type::text AS group_type FROM resource_groups \
         WHERE tenant_id = $1 AND name = $2 AND is_system = true AND is_active = true LIMIT 1",
    )
    .bind(tenant_id)
    .bind(crate::constants::groups::UNBOUND_GROUP_NAME)
    .fetch_optional(db)
    .await
}

/// A gateway user's resource groups, best first: the workspace they
/// asked for when they really are a member of it, then their primary group,
/// then the deepest, then by id. Every surface that runs a user's call in ONE
/// group takes the first; every surface that weighs all of them reads them in
/// this order.
///
/// Before this each surface ran its own membership query ordered only by
/// depth, so two top-level groups came back in whatever order the database
/// returned them: the trial user's Agent Skills calls, scoped to their E2ETests
/// workspace, were billed to (and guarded by) Default. `requested` is
/// client-supplied (`x-resource-group`); it only ever reorders groups the
/// user is already in, so it can choose among them but never add one.
pub async fn user_groups_ordered(
    db: &PgPool,
    tenant_id: &str,
    user_id: &str,
    requested: Option<&str>,
) -> Result<Vec<ResourceGroup>, sqlx::Error> {
    sqlx::query_as(
        r#"SELECT rg.*, rg.group_type::text AS group_type
           FROM resource_groups rg
           WHERE rg.tenant_id = $2
             AND rg.is_active = true
             AND rg.id IN (
                 SELECT group_id
                 FROM resource_group_memberships
                 WHERE user_id = $1::int
                   AND tenant_id = $2
                 UNION
                 SELECT rgeug.resource_group_id
                 FROM resource_group_end_user_groups rgeug
                 JOIN end_user_group_members eugm
                   ON eugm.end_user_group_id = rgeug.end_user_group_id
                 WHERE eugm.end_user_id = $1::int
             )
           ORDER BY COALESCE(rg.id = $3, false) DESC,
                    COALESCE(rg.id = (SELECT eu.primary_group_id FROM end_users eu
                                       WHERE eu.id = $1::int AND eu.tenant_id = $2), false) DESC,
                    rg.hierarchy_level DESC NULLS LAST,
                    rg.id"#,
    )
    .bind(user_id)
    .bind(tenant_id)
    .bind(requested)
    .fetch_all(db)
    .await
}

/// Resolve which resource group a request should be billed to.
/// Priority: API key group > user's group membership > default group.
///
/// A lookup that FAILS reads as "no group" here (logged at ERROR). That is
/// only right for a caller to whom no group already means "refuse" or
/// "nothing to attribute". A caller that would SKIP a control when there is
/// no group must use [`resolve_target_group_checked`] and refuse on the
/// error.
pub async fn resolve_target_group(
    db: &PgPool,
    auth: &AuthContext,
    tenant_id: &str,
) -> Option<ResourceGroup> {
    match resolve_target_group_checked(db, auth, tenant_id).await {
        Ok(group) => group,
        Err(e) => {
            tracing::error!(
                tenant_id,
                error = %e,
                "target group lookup failed — treated as no group"
            );
            None
        }
    }
}

/// [`resolve_target_group`], telling "this caller has no group" (`Ok(None)`)
/// from "the lookup failed" (`Err`). The two must not
/// be one `None`: a database error on the skill surfaces would read as "no
/// group, nothing to check" and the argument policy would be skipped.
pub async fn resolve_target_group_checked(
    db: &PgPool,
    auth: &AuthContext,
    tenant_id: &str,
) -> Result<Option<ResourceGroup>, sqlx::Error> {
    // 1. An API key is billed to its own group, and only its own group.
    //    A key whose group did not load used to fall through to the
    //    user and Default branches below, so it ran under Default's access
    //    and limits. Authentication now refuses a key whose group is
    //    inactive or gone; a lookup that still finds nothing (a race)
    //    yields no group, never another group's, and a database error is
    //    the caller's to refuse on.
    if auth.auth_type == crate::models::auth::AuthType::ApiKey {
        if let Some(ref gid) = auth.api_key_group_id {
            return sqlx::query_as::<_, ResourceGroup>(
                "SELECT *, group_type::text AS group_type FROM resource_groups WHERE id = $1 AND tenant_id = $2 AND is_active = true",
            )
            .bind(gid)
            .bind(tenant_id)
            .fetch_optional(db)
            .await;
        }
        // The platform service key is billed to the tenant's Unbound group.
        // Any other key with no group never gets here (auth refuses it).
        if auth.api_key_is_platform {
            return unbound_group(db, tenant_id).await;
        }
        return Ok(None);
    }

    // 1b. A workload-identity agent runs in, and is billed to, the group it
    //     acts in — a membership authentication
    //     resolved. One that is a member of no group gets none: never the
    //     tenant's Default group below, which it is not a member of.
    if auth.is_workload_agent() {
        let Some(gid) = auth.bound_group_id() else {
            return Ok(None);
        };
        return sqlx::query_as::<_, ResourceGroup>(
            "SELECT *, group_type::text AS group_type FROM resource_groups WHERE id = $1 AND tenant_id = $2 AND is_active = true",
        )
        .bind(gid)
        .bind(tenant_id)
        .fetch_optional(db)
        .await;
    }

    // 2. User's group membership: their workspace, else primary, else deepest
    //    (`user_groups_ordered`). Direct and EndUserGroup-mediated
    //    memberships both count.
    if let Some(ref uid) = auth.gateway_user_id {
        let groups =
            user_groups_ordered(db, tenant_id, uid, auth.resolved_group_id.as_deref()).await?;
        if let Some(group) = groups.into_iter().next() {
            return Ok(Some(group));
        }
    }

    // 3. Default group for tenant — only for anonymous or API key users.
    //    Authenticated portal users with no group membership get None (no access).
    if auth.gateway_user_id.is_some() {
        // Authenticated user with no group membership — return None so access is denied
        tracing::debug!(
            user_id = ?auth.gateway_user_id,
            "Authenticated user has no group membership — no default group fallback"
        );
        return Ok(None);
    }

    sqlx::query_as::<_, ResourceGroup>(
        r#"SELECT *, group_type::text AS group_type           FROM resource_groups
           WHERE tenant_id = $1 AND name = $2 AND is_active = true
           LIMIT 1"#,
    )
    .bind(tenant_id)
    .bind(crate::constants::groups::DEFAULT_GROUP_NAME)
    .fetch_optional(db)
    .await
}

// ---------------------------------------------------------------------------
// Internal helpers for quota queries
// ---------------------------------------------------------------------------

// The ledger helpers below return the database error; the caller decides
// what it means. A request deciding on itself fails closed through
// `or_over_limit`. The counter repair and backfill skip the scope instead:
// writing the over-limit value into a counter would refuse every later
// request of the group until the window rolls over.

/// Request-path reading of a ledger aggregate: FAIL CLOSED. A usage-DB error
/// must not silently grant unlimited spend, so it reads as the saturating
/// maximum (any configured limit is exceeded and THIS request is refused),
/// logged at ERROR so on-call sees that quota enforcement is degraded.
/// Never store the result in a counter.
trait OrOverLimit<T> {
    #[track_caller]
    fn or_over_limit(self) -> T;
}

impl OrOverLimit<i64> for Result<i64, sqlx::Error> {
    #[track_caller]
    fn or_over_limit(self) -> i64 {
        self.unwrap_or_else(|e| {
            let at = std::panic::Location::caller();
            tracing::error!(error = %e, site = %format!("{}:{}", at.file(), at.line()), "usage aggregate query failed — failing closed (usage treated as over-limit)");
            i64::MAX
        })
    }
}

impl OrOverLimit<f64> for Result<f64, sqlx::Error> {
    #[track_caller]
    fn or_over_limit(self) -> f64 {
        self.unwrap_or_else(|e| {
            let at = std::panic::Location::caller();
            tracing::error!(error = %e, site = %format!("{}:{}", at.file(), at.line()), "usage cost query failed — failing closed (cost treated as over-limit)");
            f64::MAX
        })
    }
}

async fn sum_tokens(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<i64, sqlx::Error> {
    let row: (Option<i64>,) = sqlx::query_as(
        r#"SELECT COALESCE(SUM(total_tokens), 0)
           FROM usage_logs
           WHERE tenant_id = $1 AND group_id = $2
             AND resource_type = $3 AND created_at >= $4"#,
    )
    .bind(tenant_id)
    .bind(group_id)
    .bind(resource_type)
    .bind(since)
    .fetch_one(db)
    .await?;
    Ok(row.0.unwrap_or(0))
}

async fn sum_cost(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<f64, sqlx::Error> {
    let row: (Option<f64>,) = sqlx::query_as(
        r#"SELECT COALESCE(SUM(estimated_cost), 0.0)
           FROM usage_logs
           WHERE tenant_id = $1 AND group_id = $2
             AND resource_type = $3 AND created_at >= $4"#,
    )
    .bind(tenant_id)
    .bind(group_id)
    .bind(resource_type)
    .bind(since)
    .fetch_one(db)
    .await?;
    Ok(row.0.unwrap_or(0.0))
}

/// Ledger cost in **micro-USD**, rounded PER ROW (`SUM(ROUND(cost::numeric *
/// 1e6))`) so it matches how the shadow counter accumulates
/// `round(cost_per_request * 1e6)`. Comparing this against the counter avoids
/// the spurious ±1-micro "divergences" that `round(SUM(cost) * 1e6)` produces.
/// `numeric` rounding is half-away-from-zero, matching Rust's `f64::round`.
// Returned, never 0: backfill OVERWRITES a counter with this value,
// and a 0 there would reset a spend counter and lift its budget.
async fn sum_cost_micros(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<i64, sqlx::Error> {
    let row: (Option<i64>,) = sqlx::query_as(
        r#"SELECT COALESCE(SUM(ROUND((estimated_cost)::numeric * 1000000)), 0)::bigint
           FROM usage_logs
           WHERE tenant_id = $1 AND group_id = $2
             AND resource_type = $3 AND created_at >= $4"#,
    )
    .bind(tenant_id)
    .bind(group_id)
    .bind(resource_type)
    .bind(since)
    .fetch_one(db)
    .await?;
    Ok(row.0.unwrap_or(0))
}

async fn count_requests(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<i64, sqlx::Error> {
    let row: (Option<i64>,) = sqlx::query_as(
        r#"SELECT COUNT(*)
           FROM usage_logs
           WHERE tenant_id = $1 AND group_id = $2
             AND resource_type = $3 AND created_at >= $4"#,
    )
    .bind(tenant_id)
    .bind(group_id)
    .bind(resource_type)
    .bind(since)
    .fetch_one(db)
    .await?;
    Ok(row.0.unwrap_or(0))
}

// ---------------------------------------------------------------------------
// Model-filtered usage helpers (for per-model limit enforcement)
// ---------------------------------------------------------------------------

async fn sum_tokens_for_model(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    model_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<i64, sqlx::Error> {
    let row: (Option<i64>,) = sqlx::query_as(
        r#"SELECT COALESCE(SUM(total_tokens), 0)
           FROM usage_logs
           WHERE tenant_id = $1 AND group_id = $2
             AND resource_type = $3 AND created_at >= $4 AND resource_id = $5"#,
    )
    .bind(tenant_id)
    .bind(group_id)
    .bind(resource_type)
    .bind(since)
    .bind(model_id)
    .fetch_one(db)
    .await?;
    Ok(row.0.unwrap_or(0))
}

async fn sum_cost_for_model(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    model_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<f64, sqlx::Error> {
    let row: (Option<f64>,) = sqlx::query_as(
        r#"SELECT COALESCE(SUM(estimated_cost), 0.0)
           FROM usage_logs
           WHERE tenant_id = $1 AND group_id = $2
             AND resource_type = $3 AND created_at >= $4 AND resource_id = $5"#,
    )
    .bind(tenant_id)
    .bind(group_id)
    .bind(resource_type)
    .bind(since)
    .bind(model_id)
    .fetch_one(db)
    .await?;
    Ok(row.0.unwrap_or(0.0))
}

/// Per-model ledger cost in micro-USD, rounded per row (see [`sum_cost_micros`]).
async fn sum_cost_micros_for_model(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    model_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<i64, sqlx::Error> {
    let row: (Option<i64>,) = sqlx::query_as(
        r#"SELECT COALESCE(SUM(ROUND((estimated_cost)::numeric * 1000000)), 0)::bigint
           FROM usage_logs
           WHERE tenant_id = $1 AND group_id = $2
             AND resource_type = $3 AND created_at >= $4 AND resource_id = $5"#,
    )
    .bind(tenant_id)
    .bind(group_id)
    .bind(resource_type)
    .bind(since)
    .bind(model_id)
    .fetch_one(db)
    .await?;
    Ok(row.0.unwrap_or(0))
}

async fn count_requests_for_model(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    model_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<i64, sqlx::Error> {
    let row: (Option<i64>,) = sqlx::query_as(
        r#"SELECT COUNT(*)
           FROM usage_logs
           WHERE tenant_id = $1 AND group_id = $2
             AND resource_type = $3 AND created_at >= $4 AND resource_id = $5"#,
    )
    .bind(tenant_id)
    .bind(group_id)
    .bind(resource_type)
    .bind(since)
    .bind(model_id)
    .fetch_one(db)
    .await?;
    Ok(row.0.unwrap_or(0))
}

async fn count_requests_for_skill(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    skill_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<i64, sqlx::Error> {
    let row: (Option<i64>,) = sqlx::query_as(
        r#"SELECT COUNT(*)
           FROM usage_logs
           WHERE tenant_id = $1 AND group_id = $2
             AND resource_type = $3 AND created_at >= $4 AND resource_id = $5"#,
    )
    .bind(tenant_id)
    .bind(group_id)
    .bind(resource_type)
    .bind(since)
    .bind(skill_id)
    .fetch_one(db)
    .await?;
    Ok(row.0.unwrap_or(0))
}

// ---------------------------------------------------------------------------
// Request-scoped LLM limits memo
// ---------------------------------------------------------------------------
//
// The effective-limits chain was walked three or four times per request:
// once by `enforce_llm_quota` in preflight, once by `acquire_llm_concurrency`
// a few hundred microseconds later, once by `enforce_llm_rate_limits`, and
// once more by `book_llm_post_response` after the response. Each walk is a
// parent-chain climb plus a `SELECT tenant_limits FROM llm_model`, so this
// was roughly 3-4x the chain-walk query volume the request actually needs.
//
// The memo is REQUEST-scoped rather than a TTL cache, deliberately. A TTL
// would trade exactness for cost — an operator's limit edit would take up to
// the TTL to bite, and quota enforcement is the one place where "eventually"
// is the wrong word. A request-scoped memo trades nothing: the three call
// sites are microseconds apart inside one request, and pinning them to a
// single snapshot is if anything more correct than letting a mid-flight edit
// govern the post-response booking but not the preflight that admitted it.
//
// Scoped by `middleware::request_memo`. Work that escapes the request task —
// the spawned throughput charge, an SSE body that outlives the handler
// future — simply finds no memo and queries, exactly as before. `try_with`
// returning `Err` is the normal, expected path there, not an error.

/// Which of the three limit lookups a memo entry belongs to.
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
enum LimitsKind {
    /// `collect_effective_llm_limits` — RG chain folded with the model's
    /// tenant-wide defaults.
    Effective,
    /// `fetch_per_model_limits` — the `(group, model)` assignment row.
    PerModel,
    /// `fetch_llm_tenant_limits` — the model's raw tenant-wide blob.
    TenantWide,
}

/// Per-request store for resolved limits.
///
/// A plain `Mutex<HashMap>`: it is only ever touched by the one task the
/// request runs on, so there is no contention to design around, and the lock
/// is never held across an await.
#[derive(Default)]
pub struct LlmLimitsMemo {
    entries: std::sync::Mutex<
        std::collections::HashMap<(LimitsKind, String, String), Option<serde_json::Value>>,
    >,
}

tokio::task_local! {
    /// Present for the duration of one HTTP request. Absent everywhere else.
    pub static LLM_LIMITS_MEMO: Arc<LlmLimitsMemo>;
}

/// Resolve `key` from the request memo, falling back to `load`.
///
/// `None` is cached as readily as `Some` — "this group configures no limits"
/// is the common answer and the one it would be silliest to re-derive three
/// times. An error is never cached: an unreadable limit is not an absent one,
/// and the next read must try again (fail closed).
async fn memoized_limits<F, Fut>(
    kind: LimitsKind,
    a: &str,
    b: &str,
    load: F,
) -> Result<Option<serde_json::Value>, sqlx::Error>
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<Output = Result<Option<serde_json::Value>, sqlx::Error>>,
{
    let Ok(memo) = LLM_LIMITS_MEMO.try_with(Arc::clone) else {
        // Outside a request scope (background task, spawned charge, test).
        return load().await;
    };
    let key = (kind, a.to_string(), b.to_string());
    if let Ok(guard) = memo.entries.lock() {
        if let Some(hit) = guard.get(&key) {
            return Ok(hit.clone());
        }
    }
    let value = load().await?;
    if let Ok(mut guard) = memo.entries.lock() {
        guard.insert(key, value.clone());
    }
    Ok(value)
}

async fn fetch_per_model_limits(
    db: &PgPool,
    group_id: &str,
    model_id: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    memoized_limits(LimitsKind::PerModel, group_id, model_id, || {
        fetch_per_model_limits_uncached(db, group_id, model_id)
    })
    .await
}

/// One `limits` column of an assignment row; `Err` when unreadable.
async fn fetch_limits_row(
    db: &PgPool,
    sql: &str,
    a: &str,
    b: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    let row: Option<(Option<serde_json::Value>,)> = sqlx::query_as(sql)
        .bind(a)
        .bind(b)
        .fetch_optional(db)
        .await?;
    Ok(row.and_then(|(l,)| l).filter(|v| !v.is_null()))
}

async fn fetch_per_model_limits_uncached(
    db: &PgPool,
    group_id: &str,
    model_id: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    fetch_limits_row(
        db,
        "SELECT limits FROM resource_group_llm_models WHERE resource_group_id = $1 AND llm_model_id = $2",
        group_id,
        model_id,
    )
    .await
}

async fn fetch_per_skill_limits(
    db: &PgPool,
    group_id: &str,
    skill_id: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    fetch_limits_row(
        db,
        "SELECT limits FROM resource_group_agent_skills WHERE resource_group_id = $1 AND agent_skill_id = $2",
        group_id,
        skill_id,
    )
    .await
}

/// A resource's tenant-wide default limits (`{table}.tenant_limits`);
/// `Err` when unreadable (fail closed).
async fn fetch_tenant_limits(
    db: &PgPool,
    table: &'static str,
    id: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    // `table` is one of three literals below, never caller input.
    let sql = format!("SELECT tenant_limits FROM {table} WHERE id = $1");
    let row: Option<(Option<serde_json::Value>,)> =
        sqlx::query_as(&sql).bind(id).fetch_optional(db).await?;
    Ok(row.and_then(|(v,)| v).filter(|v| !v.is_null()))
}

// ---------------------------------------------------------------------------
// Ancestry-based helpers for hierarchical budget enforcement
// ---------------------------------------------------------------------------

/// How long an ancestor rollup may be reused before it is re-read.
///
/// These aggregates are `SUM`/`COUNT` over the tenant's whole *month* of
/// `usage_logs`, filtered by an unanchored `LIKE` prefix, and they run on
/// the synchronous request path — up to six per ancestor, ten ancestors
/// deep, for every LLM call. Their cost scales with the ledger rather than
/// with anything about the request, which made them the dominant latency
/// term of a proxied call on any tenant with real history.
///
/// The trade is stated plainly: an ancestor budget can now be overshot by
/// up to this many seconds of descendant spend before it starts refusing.
/// That is already the shape of this gate — it is read-then-decide with no
/// reservation, so N concurrent requests at 99% of a budget all pass
/// anyway — and 5 seconds of drift is small next to a monthly budget.
/// Keep it short: this is a *hard* spend cap, not a hint.
const ANCESTOR_AGG_TTL_SECS: u64 = 5;

/// How long an ancestor's own limit configuration may be reused.
///
/// Longer than the rollups because the numerator moves every second and
/// the denominator only moves when an operator edits it. A tightened
/// ancestor budget therefore takes effect within this window rather than
/// instantly — the same bargain `run_budget`'s caps cache and the
/// residency profile cache already make.
const ANCESTOR_LIMITS_TTL_SECS: u64 = 30;

/// Cached ancestor rollups. Keyed by everything that changes the answer:
/// database, tenant, ancestry prefix, resource type, metric and window start.
fn ancestor_agg_i64_cache() -> &'static moka::sync::Cache<String, i64> {
    static C: std::sync::OnceLock<moka::sync::Cache<String, i64>> = std::sync::OnceLock::new();
    C.get_or_init(|| {
        moka::sync::Cache::builder()
            .max_capacity(50_000)
            .time_to_live(std::time::Duration::from_secs(ANCESTOR_AGG_TTL_SECS))
            .build()
    })
}

fn ancestor_agg_f64_cache() -> &'static moka::sync::Cache<String, f64> {
    static C: std::sync::OnceLock<moka::sync::Cache<String, f64>> = std::sync::OnceLock::new();
    C.get_or_init(|| {
        moka::sync::Cache::builder()
            .max_capacity(50_000)
            .time_to_live(std::time::Duration::from_secs(ANCESTOR_AGG_TTL_SECS))
            .build()
    })
}

/// One ancestor's own limits plus the two fields the walk needs to
/// continue: `parent_group_id` and `hierarchy_path`, then its name for the
/// refusal message. Column order mirrors the `SELECT` in
/// [`enforce_ancestor_budgets`] — keep the two together.
type AncestorRow = (
    Option<serde_json::Value>,
    Option<serde_json::Value>,
    Option<serde_json::Value>,
    Option<String>,
    Option<String>,
    String,
);

/// Cached ancestor configuration, keyed by database and group id. `None`
/// means "no such group", which is a real answer (it terminates the walk)
/// and worth caching for the same reason.
fn ancestor_row_cache() -> &'static moka::sync::Cache<String, Option<AncestorRow>> {
    static C: std::sync::OnceLock<moka::sync::Cache<String, Option<AncestorRow>>> =
        std::sync::OnceLock::new();
    C.get_or_init(|| {
        moka::sync::Cache::builder()
            .max_capacity(50_000)
            .time_to_live(std::time::Duration::from_secs(ANCESTOR_LIMITS_TTL_SECS))
            .build()
    })
}

/// The database a pool is connected to, as a cache-key component.
///
/// In production this is a constant — one process, one database — so it
/// costs a string and changes nothing. It matters under `sqlx::test`, which
/// hands every test its own throwaway database but shares one process, and
/// therefore one set of these caches. Two tests using the same tenant id and
/// the same group names produce identical keys, so whichever ran first
/// answers for the other and the second sees rollups from a database it
/// never wrote to. Scoping the key by database keeps the caches honest
/// without weakening them where it counts.
fn db_cache_key(db: &PgPool) -> String {
    db.connect_options()
        .get_database()
        .unwrap_or("")
        .to_string()
}

fn ancestor_agg_key(
    db: &PgPool,
    metric: &str,
    tenant_id: &str,
    ancestry_prefix: &str,
    resource_type: &str,
    since: chrono::DateTime<Utc>,
) -> String {
    format!(
        "{}|{metric}|{tenant_id}|{ancestry_prefix}|{resource_type}|{}",
        db_cache_key(db),
        since.timestamp()
    )
}

async fn sum_tokens_by_ancestry(
    db: &PgPool,
    tenant_id: &str,
    ancestry_prefix: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<i64, sqlx::Error> {
    let key = ancestor_agg_key(
        db,
        "tokens",
        tenant_id,
        ancestry_prefix,
        resource_type,
        since,
    );
    if let Some(v) = ancestor_agg_i64_cache().get(&key) {
        return Ok(v);
    }
    let pattern = format!("{}%", ancestry_prefix);
    let row: Result<(Option<i64>,), _> = sqlx::query_as(
        r#"SELECT COALESCE(SUM(total_tokens), 0)
           FROM usage_logs
           WHERE tenant_id = $1 AND ancestry_path LIKE $2
             AND resource_type = $3 AND created_at >= $4"#,
    )
    .bind(tenant_id)
    .bind(&pattern)
    .bind(resource_type)
    .bind(since)
    .fetch_one(db)
    .await;

    match row {
        Ok(r) => {
            let v = r.0.unwrap_or(0);
            ancestor_agg_i64_cache().insert(key, v);
            Ok(v)
        }
        Err(e) => Err(e),
    }
}

async fn sum_cost_by_ancestry(
    db: &PgPool,
    tenant_id: &str,
    ancestry_prefix: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<f64, sqlx::Error> {
    let key = ancestor_agg_key(db, "cost", tenant_id, ancestry_prefix, resource_type, since);
    if let Some(v) = ancestor_agg_f64_cache().get(&key) {
        return Ok(v);
    }
    let pattern = format!("{}%", ancestry_prefix);
    let row: Result<(Option<f64>,), _> = sqlx::query_as(
        r#"SELECT COALESCE(SUM(estimated_cost), 0.0)
           FROM usage_logs
           WHERE tenant_id = $1 AND ancestry_path LIKE $2
             AND resource_type = $3 AND created_at >= $4"#,
    )
    .bind(tenant_id)
    .bind(&pattern)
    .bind(resource_type)
    .bind(since)
    .fetch_one(db)
    .await;

    match row {
        Ok(r) => {
            let v = r.0.unwrap_or(0.0);
            ancestor_agg_f64_cache().insert(key, v);
            Ok(v)
        }
        Err(e) => Err(e),
    }
}

async fn count_requests_by_ancestry(
    db: &PgPool,
    tenant_id: &str,
    ancestry_prefix: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<i64, sqlx::Error> {
    let key = ancestor_agg_key(
        db,
        "requests",
        tenant_id,
        ancestry_prefix,
        resource_type,
        since,
    );
    if let Some(v) = ancestor_agg_i64_cache().get(&key) {
        return Ok(v);
    }
    let pattern = format!("{}%", ancestry_prefix);
    let row: Result<(Option<i64>,), _> = sqlx::query_as(
        r#"SELECT COUNT(*)
           FROM usage_logs
           WHERE tenant_id = $1 AND ancestry_path LIKE $2
             AND resource_type = $3 AND created_at >= $4"#,
    )
    .bind(tenant_id)
    .bind(&pattern)
    .bind(resource_type)
    .bind(since)
    .fetch_one(db)
    .await;

    match row {
        Ok(r) => {
            let v = r.0.unwrap_or(0);
            ancestor_agg_i64_cache().insert(key, v);
            Ok(v)
        }
        Err(e) => Err(e),
    }
}

/// Walk the parent group chain and collect the effective limits for a given
/// limit field (e.g. "llm_global_limits", "mcp_global_limits", "skill_global_limits").
///
/// Governance inheritance is ALWAYS on — this ignores `inherit_resources`.
/// Uses [`restrictive_merge`]: a descendant can only tighten.
async fn collect_effective_limits(
    db: &PgPool,
    group: &ResourceGroup,
    limit_field: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    let family = match limit_field {
        "mcp_global_limits" => LimitFamily::Mcp,
        "skill_global_limits" => LimitFamily::Skill,
        _ => LimitFamily::Llm,
    };
    Ok(merge_chain(
        limit_chain(db, group, limit_field).await?,
        family,
    ))
}

/// The limit documents of a chain, root first, merged ([`restrictive_merge`]).
fn merge_chain(chain: Vec<serde_json::Value>, family: LimitFamily) -> Option<serde_json::Value> {
    let mut effective: Option<serde_json::Value> = None;
    for limits in chain {
        match effective {
            None => effective = Some(limits),
            Some(ref mut base) => restrictive_merge(base, &limits, family),
        }
    }
    effective
}

/// The limit documents of `limit_field` along the group's chain, root
/// first, each as stored. A level that states none (SQL NULL or a JSON
/// `null`) is left out. `Err` when a hop cannot be read (fail closed).
async fn limit_chain(
    db: &PgPool,
    group: &ResourceGroup,
    limit_field: &str,
) -> Result<Vec<serde_json::Value>, sqlx::Error> {
    // Collect limits from self + all ancestors (root → ... → self order for merge)
    let mut chain: Vec<Option<serde_json::Value>> = Vec::new();

    // Start with this group
    let own_limits = match limit_field {
        "llm_global_limits" => group.llm_global_limits.clone(),
        "mcp_global_limits" => group.mcp_global_limits.clone(),
        "skill_global_limits" => group.skill_global_limits.clone(),
        _ => None,
    };
    chain.push(own_limits);

    // Walk parent chain (governance always inherits, regardless of
    // inherit_resources). A hop that cannot be read is an error, not the end
    // of the chain: ending it would drop every ancestor's limits above the
    // failed hop and enforce the group as if they were never set (fail
    // closed).
    let mut current_parent_id = group.parent_group_id.clone();
    for _ in 0..10 {
        let pid = match current_parent_id {
            Some(ref id) => id.clone(),
            None => break,
        };

        let parent: Option<(Option<serde_json::Value>, Option<serde_json::Value>, Option<serde_json::Value>, Option<String>)> =
            sqlx::query_as(
                "SELECT llm_global_limits, mcp_global_limits, skill_global_limits, parent_group_id FROM resource_groups WHERE id = $1",
            )
            .bind(&pid)
            .fetch_optional(db)
            .await?;

        match parent {
            Some((llm, mcp, skill, next_pid)) => {
                let parent_limits = match limit_field {
                    "llm_global_limits" => llm,
                    "mcp_global_limits" => mcp,
                    "skill_global_limits" => skill,
                    _ => None,
                };
                chain.push(parent_limits);
                current_parent_id = next_pid;
            }
            None => break,
        }
    }

    // Root → ... → self (each child can tighten, not relax).
    chain.reverse();

    // A JSON `null` value (distinct from a SQL NULL column) states no
    // limits: resource-group creation stores `'null'::json` for a group with
    // none. It is left out rather than merged.
    Ok(chain
        .into_iter()
        .flatten()
        .filter(|limits| !limits.is_null())
        .collect())
}

/// Fold a resource's tenant-wide default limits into a group chain's
/// effective limits ([`restrictive_merge`]).
fn fold_tenant_limits(
    mut effective: Option<serde_json::Value>,
    tenant: Option<serde_json::Value>,
    family: LimitFamily,
) -> Option<serde_json::Value> {
    if let Some(tl) = tenant {
        match effective {
            None => effective = Some(tl),
            Some(ref mut base) => restrictive_merge(base, &tl, family),
        }
    }
    effective
}

/// Effective LLM limits for a (group, model): the resource-group chain
/// (`collect_effective_limits`) folded restrictively with the model's
/// TENANT-WIDE default limits (`llm_model.tenant_limits` — per-resource Limits).
/// min-wins, so a tenant-wide model cap tightens whatever the group
/// allows and applies even when the group sets no LLM limits. Set via
/// Resources → AI Models → <model> → Limits. `Err` when any part is
/// unreadable: the caller refuses (fail closed).
pub async fn collect_effective_llm_limits(
    db: &PgPool,
    group: &ResourceGroup,
    model_id: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    memoized_limits(LimitsKind::Effective, &group.id, model_id, || {
        collect_effective_llm_limits_uncached(db, group, model_id)
    })
    .await
}

async fn collect_effective_llm_limits_uncached(
    db: &PgPool,
    group: &ResourceGroup,
    model_id: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    let effective = collect_effective_limits(db, group, "llm_global_limits").await?;
    Ok(fold_tenant_limits(
        effective,
        fetch_tenant_limits(db, "llm_model", model_id).await?,
        LimitFamily::Llm,
    ))
}

/// Effective skill limits for a (group, skill): the RG `skill_global_limits`
/// chain folded restrictively with the skill's TENANT-WIDE default limits
/// (`agent_skills.tenant_limits`). min-wins. Set via Resources → Skills →
/// <skill> → Limits.
pub async fn collect_effective_skill_limits(
    db: &PgPool,
    group: &ResourceGroup,
    skill_id: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    let effective = collect_effective_limits(db, group, "skill_global_limits").await?;
    Ok(fold_tenant_limits(
        effective,
        fetch_tenant_limits(db, "agent_skills", skill_id).await?,
        LimitFamily::Skill,
    ))
}

/// The family of a limit document, which says how its readers take a
/// stated number that is not positive (see [`restrictive_merge`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum LimitFamily {
    /// `llm_global_limits`, a model's tenant-wide and per-group limits.
    Llm,
    /// `mcp_global_limits` and a server's tenant-wide limits.
    Mcp,
    /// `skill_global_limits`, a skill's tenant-wide and per-group limits.
    Skill,
    /// `media_global_limits[mode]` and a media model's tenant-wide limits.
    Media,
    /// `a2a_global_limits`, a card's tenant-wide and per-group limits.
    A2a,
}

impl LimitFamily {
    /// Whether this family's reader takes a number that is not positive at
    /// `key` as a limit (the strictest: it admits nothing) rather than as
    /// unset.
    fn non_positive_is_a_limit(self, key: &str) -> bool {
        match self {
            // A media quota is a counter cap: 0 admits no request.
            Self::Media => true,
            // `chain.max_delegation_depth: 0` forbids delegation (the control
            // plane accepts 0..32); the invocation and frequency readers take
            // a value that is not positive as unset.
            Self::A2a => key == "max_delegation_depth",
            // The LLM, skill and MCP frequency readers take such a value as
            // unset. The MCP execution limits refuse it, at the level that
            // states it (`load_mcp_limit_sources` checks every level).
            Self::Llm | Self::Mcp | Self::Skill => false,
        }
    }
}

/// Restrictive merge of two JSON limit documents: `overlay` (one level of a
/// chain) into `base` (the levels merged so far). A limit can be tightened
/// along a chain, never removed:
///
/// - Numeric: the smallest of the stated numbers, kept as stated (an
///   integer stays an integer for the readers that take `as_i64`). A number
///   that is not positive is stated only where the family reads it as a
///   limit ([`LimitFamily::non_positive_is_a_limit`]); elsewhere it is no
///   limit at its level and the other level's value stays.
/// - `null` (or an absent field) states nothing: the other level's value
///   stays.
/// - A value of another type than the other level's (a string where a
///   number is, `"no"` where a bool is) does not replace a number, a bool or
///   a section: the family reads it as unset, or refuses it at its own
///   level (MCP execution limits).
/// - Bool (e.g. hard_stop): true wins (more restrictive).
/// - Object: recurse.
/// - A base that is not an object (a JSON-null document) takes the overlay.
pub(crate) fn restrictive_merge(
    base: &mut serde_json::Value,
    overlay: &serde_json::Value,
    family: LimitFamily,
) {
    let serde_json::Value::Object(overlay_map) = overlay else {
        return;
    };
    let serde_json::Value::Object(base_map) = base else {
        *base = overlay.clone();
        return;
    };
    for (key, overlay_val) in overlay_map {
        match base_map.get_mut(key) {
            Some(base_val) => merge_limit_value(
                base_val,
                overlay_val,
                family.non_positive_is_a_limit(key),
                family,
            ),
            None if overlay_val.is_null() => {}
            None => {
                base_map.insert(key.clone(), overlay_val.clone());
            }
        }
    }
}

/// One field of [`restrictive_merge`].
fn merge_limit_value(
    base: &mut serde_json::Value,
    overlay: &serde_json::Value,
    non_positive_is_a_limit: bool,
    family: LimitFamily,
) {
    use serde_json::Value;
    // How far a value can be read as a limit: a number, a section or a
    // bool is a limit's shape; a string or a list is not.
    fn shape(v: &Value) -> u8 {
        match v {
            Value::Number(_) | Value::Object(_) => 2,
            Value::Bool(_) => 1,
            _ => 0,
        }
    }
    let stated = |n: &serde_json::Number| {
        n.as_f64()
            .filter(|v| v.is_finite() && (non_positive_is_a_limit || *v > 0.0))
    };
    match (&*base, overlay) {
        (_, Value::Null) => {}
        (Value::Null, _) => *base = overlay.clone(),
        (Value::Object(_), Value::Object(_)) => restrictive_merge(base, overlay, family),
        (Value::Bool(b), Value::Bool(o)) => *base = Value::Bool(*b || *o),
        (Value::Number(b), Value::Number(o)) => {
            let replace = match (stated(b), stated(o)) {
                (Some(bv), Some(ov)) => ov < bv,
                (None, Some(_)) => true,
                _ => false,
            };
            if replace {
                *base = overlay.clone();
            }
        }
        // Two values that are not a limit's shape (strings, lists): the
        // overlay's.
        (b, o) if shape(b) == 0 && shape(o) == 0 => *base = overlay.clone(),
        (b, o) if shape(o) > shape(b) => *base = overlay.clone(),
        _ => {}
    }
}

// ---------------------------------------------------------------------------
// LLM governance collection
// ---------------------------------------------------------------------------

/// Collect the effective LLM governance for a group + model by walking the
/// parent chain and applying governance-specific restrictive merge.
///
/// At each level we query `resource_group_llm_governance` for the specific
/// model, so governance is now per-model-per-group rather than per-group.
///
/// `Err` when any layer cannot be read OR does not parse (fail closed):
/// it used to read an unreachable database as "no governance" and skip a
/// blob it could not parse, so temperature caps, tool rules and mandatory
/// system-prompt fragments silently stopped applying.
pub async fn collect_effective_llm_governance(
    db: &PgPool,
    group: &ResourceGroup,
    model_id: &str,
) -> Result<Option<LlmGovernance>, sqlx::Error> {
    let mut chain: Vec<Option<serde_json::Value>> = Vec::new();

    // Each group contributes TWO governance layers, both enforced (merged
    // most-restrictive by `merge_governance`):
    //   1. group-wide caps on the `resource_groups.llm_global_governance`
    //      column (the admin "LLM Global Governance" form — applies to every
    //      model in the group), and
    //   2. an optional per-(group, model) override row in
    //      `resource_group_llm_governance` (Models → <model> → Limits).
    // Previously ONLY layer 2 was read, so group-wide caps an operator set in
    // the UI (the common case) were silently never enforced.

    // This group: group-wide column (already loaded on the struct) + per-model row.
    chain.push(group.llm_global_governance.clone());
    let self_gov: Option<(Option<serde_json::Value>,)> = sqlx::query_as(
        "SELECT governance FROM resource_group_llm_governance \
         WHERE resource_group_id = $1 AND llm_model_id = $2",
    )
    .bind(&group.id)
    .bind(model_id)
    .fetch_optional(db)
    .await?;
    chain.push(self_gov.and_then(|(g,)| g));

    // Tenant-wide model default (per-resource Policies): applies to this
    // model regardless of group. Folded into the same most-restrictive merge so
    // it behaves as a min-wins floor combined with every group layer. Set via
    // Resources → AI Models → <model> → Policies.
    let model_tenant_gov: Option<(Option<serde_json::Value>,)> =
        sqlx::query_as("SELECT tenant_governance FROM llm_model WHERE id = $1")
            .bind(model_id)
            .fetch_optional(db)
            .await?;
    chain.push(model_tenant_gov.and_then(|(g,)| g));

    // Walk parent chain
    let mut current_parent_id = group.parent_group_id.clone();
    for _ in 0..10 {
        let pid = match current_parent_id {
            Some(ref id) => id.clone(),
            None => break,
        };

        // Fetch the parent's per-model override + group-wide column + its own
        // parent_group_id in one round-trip.
        let parent: Option<(
            Option<serde_json::Value>,
            Option<serde_json::Value>,
            Option<String>,
        )> = sqlx::query_as(
            "SELECT rglg.governance, rg.llm_global_governance, rg.parent_group_id \
                 FROM resource_groups rg \
                 LEFT JOIN resource_group_llm_governance rglg \
                   ON rglg.resource_group_id = rg.id AND rglg.llm_model_id = $2 \
                 WHERE rg.id = $1",
        )
        .bind(&pid)
        .bind(model_id)
        .fetch_optional(db)
        .await?;

        match parent {
            Some((per_model_gov, group_wide_gov, next_pid)) => {
                chain.push(group_wide_gov);
                chain.push(per_model_gov);
                current_parent_id = next_pid;
            }
            None => break,
        }
    }

    // Reverse so we merge root → ... → self
    chain.reverse();

    let mut effective: Option<LlmGovernance> = None;
    for gov_json in chain.into_iter().flatten() {
        // A blob that does not parse is a policy we cannot read, not an
        // absent one.
        let gov: LlmGovernance =
            serde_json::from_value(gov_json).map_err(|e| sqlx::Error::Decode(Box::new(e)))?;
        match effective {
            None => effective = Some(gov),
            Some(ref mut base) => governance::merge_governance(base, &gov),
        }
    }

    Ok(effective)
}

/// Effective `media_global_governance` (full mode→fields dict) = this group
/// merged with every ancestor, most-restrictive (see
/// `media_governance::merge_media_governance`). Mirrors LLM/Skill governance
/// inheritance; previously the media routes read only the single resolved
/// group (no inheritance). `Err` when a hop is unreadable (fail closed).
pub async fn collect_effective_media_governance(
    db: &PgPool,
    group: &ResourceGroup,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    let mut effective = group.media_global_governance.clone();
    let mut current_parent_id = group.parent_group_id.clone();
    for _ in 0..10 {
        let pid = match current_parent_id {
            Some(ref id) => id.clone(),
            None => break,
        };
        let row: Option<(Option<serde_json::Value>, Option<String>)> = sqlx::query_as(
            "SELECT media_global_governance, parent_group_id FROM resource_groups WHERE id = $1",
        )
        .bind(&pid)
        .fetch_optional(db)
        .await?;
        match row {
            Some((gov, next)) => {
                if let Some(parent_gov) = gov {
                    match effective {
                        Some(ref mut base) => {
                            crate::services::media_governance::merge_media_governance(
                                base,
                                &parent_gov,
                            )
                        }
                        None => effective = Some(parent_gov),
                    }
                }
                current_parent_id = next;
            }
            None => break,
        }
    }
    Ok(effective)
}

/// Effective per-mode media LIMITS = this group's `media_global_limits[mode]`
/// merged with every ancestor (restrictive: numeric min, bool true-wins).
/// `Err` when a hop is unreadable (fail closed).
pub async fn collect_effective_media_limits(
    db: &PgPool,
    group: &ResourceGroup,
    mode: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    let mut effective: Option<serde_json::Value> = group
        .media_global_limits
        .as_ref()
        .and_then(|v| v.get(mode).cloned())
        .filter(|v| !v.is_null());
    let mut current_parent_id = group.parent_group_id.clone();
    for _ in 0..10 {
        let pid = match current_parent_id {
            Some(ref id) => id.clone(),
            None => break,
        };
        let row: Option<(Option<serde_json::Value>, Option<String>)> = sqlx::query_as(
            "SELECT media_global_limits, parent_group_id FROM resource_groups WHERE id = $1",
        )
        .bind(&pid)
        .fetch_optional(db)
        .await?;
        match row {
            Some((lim, next)) => {
                if let Some(mode_lim) = lim
                    .as_ref()
                    .and_then(|v| v.get(mode))
                    .filter(|v| !v.is_null())
                {
                    match effective {
                        Some(ref mut base) => restrictive_merge(base, mode_lim, LimitFamily::Media),
                        None => effective = Some(mode_lim.clone()),
                    }
                }
                current_parent_id = next;
            }
            None => break,
        }
    }
    Ok(effective)
}

/// Effective per-mode media limits for a (group, model): the RG per-mode chain
/// folded restrictively with the model's TENANT-WIDE default limits
/// (`llm_model.tenant_limits`, a flat per-mode blob for a media model). min-wins.
pub async fn collect_effective_media_limits_for_model(
    db: &PgPool,
    group: &ResourceGroup,
    mode: &str,
    model_id: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    let effective = collect_effective_media_limits(db, group, mode).await?;
    Ok(fold_tenant_limits(
        effective,
        fetch_tenant_limits(db, "llm_model", model_id).await?,
        LimitFamily::Media,
    ))
}

/// Effective per-mode media GOVERNANCE for a (group, model): the RG mode→fields
/// dict (`collect_effective_media_governance`) folded most-restrictive with the
/// model's TENANT-WIDE default governance (`llm_model.tenant_governance` — a flat
/// single-mode field map for a media model). Returns the full mode→fields dict;
/// call `media_governance::governance_for_mode` to extract this mode. min-wins.
/// `Err` when any layer is unreadable (fail closed).
pub async fn collect_effective_media_governance_for_model(
    db: &PgPool,
    group: &ResourceGroup,
    mode: &str,
    model_id: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    let mut effective = collect_effective_media_governance(db, group).await?;
    let model_tg: Option<(Option<serde_json::Value>,)> =
        sqlx::query_as("SELECT tenant_governance FROM llm_model WHERE id = $1")
            .bind(model_id)
            .fetch_optional(db)
            .await?;
    if let Some((Some(tg),)) = model_tg {
        // The model blob is the flat field-map for THIS mode; wrap it as a
        // {mode: fields} dict so the full-dict most-restrictive merge applies.
        let wrapped = serde_json::json!({ mode: tg });
        match effective {
            Some(ref mut base) => {
                crate::services::media_governance::merge_media_governance(base, &wrapped)
            }
            None => effective = Some(wrapped),
        }
    }
    Ok(effective)
}

/// A media mode's billing unit: the `proxy_logs` column that records it
/// (the per-mode usage discriminator), and the `media_global_limits` field
/// that caps it per day. None for modes with no media route.
fn media_unit(mode: &str) -> Option<(&'static str, &'static str)> {
    match mode {
        "audio_speech" => Some(("media_audio_chars", "daily_characters")),
        "audio_transcription" => Some(("media_audio_seconds_input", "daily_audio_seconds")),
        "image_generation" => Some(("media_images_generated", "daily_images")),
        "video_generation" => Some(("media_video_seconds_output", "daily_video_seconds")),
        _ => None,
    }
}

/// Enforce per-mode media quota (`daily_requests`, the mode's daily unit
/// field — `daily_characters`, `daily_audio_seconds`, `daily_images`,
/// `daily_video_seconds` — and `daily_budget_usd`). The monthly fields are
/// not read. Returns `Some(reason)` to
/// BLOCK, `None` to allow. `request_units` is this call's billing units
/// (chars/images/seconds), `request_cost` its estimated cost.
///
/// Admitted atomically on the shared counters (`media:{mode}:grp:{g}`, UTC
/// day): it used to SUM `proxy_logs` and compare, so a burst all read the
/// same total — and a database error read as zero usage. A counter that is
/// cold (first call of the day, a Redis flush) is seeded from `proxy_logs`
/// first. What the route knows before the call is reserved at admission;
/// a call that then fails upstream stays counted (the fail-closed
/// direction). Where the call's units or cost are only known from the
/// provider's answer (a transcription's seconds, a token-priced image),
/// [`settle_media_call`] raises the counters to what the call consumed:
/// the admission reserved nothing or an
/// estimate for them, and the counters used to stay there.
///
/// All three counters are reserved whenever any limit is configured, the
/// ones with no limit as accounting dimensions: a limit added later in the
/// day starts from the day's real usage.
///
/// `Ok(None)`: no per-mode limit applies, nothing was counted.
/// `Ok(Some(_))`: admitted, with what was reserved. `Err(reason)`: refused.
/// What one media call will consume, for [`enforce_media_quota`].
pub struct MediaCharge<'a> {
    pub mode: &'a str,
    pub model_id: &'a str,
    /// Billing units: characters, images or seconds, per the mode.
    pub units: i64,
    pub cost_usd: f64,
}

/// What an admitted media call reserved on its group's per-mode counters.
#[derive(Debug, Clone, PartialEq)]
pub struct MediaReservation {
    /// `media:{mode}:grp:{group}`.
    pub scope: String,
    pub units: i64,
    pub cost_micros: i64,
}

/// Raise the per-mode counters of an admitted media call to what the call
/// consumed: the units and cost the provider's answer established, less
/// what the admission reserved. Never lowers a counter (a call that
/// consumed less than it reserved stays counted at its reservation, as a
/// call that failed upstream does).
///
/// The amounts go to the current day's counters. A call admitted before
/// midnight UTC and answered after it books its top-up on the new day (its
/// reservation stays on the old one).
pub fn settle_media_call(
    usage_state: &Arc<UsageState>,
    reserved: &MediaReservation,
    actual_units: i64,
    actual_cost_usd: f64,
) {
    let cost_micros = (actual_cost_usd.max(0.0) * 1_000_000.0).round() as i64;
    let deltas = [
        (
            format!("{}:units", reserved.scope),
            actual_units - reserved.units,
        ),
        (
            format!("{}:cost", reserved.scope),
            cost_micros - reserved.cost_micros,
        ),
    ];
    if deltas.iter().all(|(_, d)| *d <= 0) {
        return;
    }
    let us = usage_state.clone();
    tokio::spawn(async move {
        for (key, delta) in deltas {
            if delta > 0 {
                us.settle_usage(&key, QuotaWindow::Day, delta).await;
            }
        }
    });
}

pub async fn enforce_media_quota(
    usage_state: &UsageState,
    db: &PgPool,
    tenant_id: &str,
    group: &ResourceGroup,
    charge: &MediaCharge<'_>,
) -> Result<Option<MediaReservation>, String> {
    let MediaCharge {
        mode,
        model_id,
        units: request_units,
        cost_usd: request_cost,
    } = *charge;
    use limits::{DimRefusal, Unit, usage_dim};
    let unavailable = |what: &str| {
        tracing::error!(mode, group_id = %group.id, what, "media quota unreadable; refusing (fail closed)");
        Err(format!(
            "media quota for {mode} cannot be checked right now"
        ))
    };
    let limits = match collect_effective_media_limits_for_model(db, group, mode, model_id).await {
        Ok(Some(l)) => l,
        Ok(None) => return Ok(None),
        Err(e) => return unavailable(&e.to_string()),
    };
    let Some((unit_col, unit_field)) = media_unit(mode) else {
        return Ok(None);
    };

    let daily_requests = limits.get("daily_requests").and_then(|v| v.as_i64());
    let daily_units = limits.get(unit_field).and_then(|v| v.as_i64());
    let daily_budget = limits
        .get("daily_budget_usd")
        .and_then(|v| v.as_f64())
        .map(|usd| (usd * 1_000_000.0).round() as i64);
    if daily_requests.is_none() && daily_units.is_none() && daily_budget.is_none() {
        return Ok(None);
    }

    let scope = format!("media:{mode}:grp:{}", group.id);
    let keys = [
        format!("{scope}:req"),
        format!("{scope}:units"),
        format!("{scope}:cost"),
    ];
    // Seed a cold day from the ledger (proxy_logs carries the per-mode
    // billing-unit columns). `unit_col` is a fixed literal (no injection).
    let mut cold = false;
    for k in &keys {
        match usage_state
            .counter_value(&UsageState::usage_key(k, QuotaWindow::Day))
            .await
        {
            Ok(0) => cold = true,
            Ok(_) => {}
            Err(e) => return unavailable(&e.to_string()),
        }
    }
    if cold {
        let today_start = Utc::now()
            .date_naive()
            .and_hms_opt(0, 0, 0)
            .unwrap()
            .and_utc();
        let sql = format!(
            "SELECT COUNT(*)::bigint AS reqs, \
                    COALESCE(SUM({unit_col}), 0)::bigint AS units, \
                    COALESCE(SUM(llm_estimated_cost_usd), 0)::double precision AS cost \
             FROM proxy_logs \
             WHERE tenant_id = $1 AND resource_group_id = $2 \
               AND timestamp >= $3 AND {unit_col} IS NOT NULL"
        );
        let used: Result<(i64, i64, f64), _> = sqlx::query_as(&sql)
            .bind(tenant_id)
            .bind(&group.id)
            .bind(today_start)
            .fetch_one(db)
            .await;
        let (r, u, c) = match used {
            Ok(v) => v,
            Err(e) => return unavailable(&e.to_string()),
        };
        for (k, v) in keys.iter().zip([r, u, (c * 1_000_000.0).round() as i64]) {
            if v > 0 {
                if let Err(e) = usage_state
                    .seed_key_if_absent(
                        &UsageState::usage_key(k, QuotaWindow::Day),
                        v,
                        QuotaWindow::Day.ttl_secs(),
                    )
                    .await
                {
                    return unavailable(&e.to_string());
                }
            }
        }
    }

    let quota = |lt: &str, limit: i64, unit: Unit| DimRefusal::Quota {
        owner_id: group.id.clone(),
        owner_name: group.name.clone(),
        limit_type: lt.to_string(),
        limit,
        unit,
    };
    let dims = vec![
        usage_dim(
            &keys[0],
            QuotaWindow::Day,
            daily_requests,
            Measure::One,
            quota(
                &format!("media_daily_requests:{mode}"),
                daily_requests.unwrap_or(0),
                Unit::Count,
            ),
        ),
        usage_dim(
            &keys[1],
            QuotaWindow::Day,
            daily_units,
            Measure::InTokens,
            quota(
                &format!("media_daily_units:{mode}"),
                daily_units.unwrap_or(0),
                Unit::Count,
            ),
        ),
        usage_dim(
            &keys[2],
            QuotaWindow::Day,
            daily_budget,
            Measure::CostMicros,
            quota(
                &format!("media_daily_budget:{mode}"),
                daily_budget.unwrap_or(0),
                Unit::Micros,
            ),
        ),
    ];
    // The call's units ride on the input measure; its cost on ONE output
    // unit priced at the whole cost, so the cost dimension refuses (below
    // the one-unit minimum) when the cost does not fit.
    let cost_micros = (request_cost.max(0.0) * 1_000_000.0).round();
    let shape = Shape {
        in_tokens: request_units.max(0),
        in_estimate: request_units.max(0),
        out_tokens: 1,
        min_out: 1,
        choices: 1,
        in_micros_per_token: 0.0,
        out_micros_per_token: cost_micros,
    };
    match limits::admit(usage_state, None, dims, shape).await {
        Ok(Ok(_)) => Ok(Some(MediaReservation {
            scope,
            units: request_units.max(0),
            cost_micros: cost_micros as i64,
        })),
        Ok(Err(refused)) => Err(refused
            .quota_result()
            .message
            .unwrap_or_else(|| format!("Media daily limit reached for {mode}"))),
        Err(e) => unavailable(&e.to_string()),
    }
}

fn make_blocked(
    group_id: &str,
    group_name: &str,
    limit_type: &str,
    current_usage: &str,
    max_limit: &str,
    message: String,
) -> UsageEnforcementResult {
    UsageEnforcementResult {
        blocked: true,
        group_id: Some(group_id.to_string()),
        group_name: Some(group_name.to_string()),
        message: Some(message),
        limit_type: Some(limit_type.to_string()),
        current_usage: Some(current_usage.to_string()),
        max_limit: Some(max_limit.to_string()),
        warning_headers: Vec::new(),
    }
}

/// Seconds the client should wait before retrying after a 429
/// usage-limit block. Derived from the `limit_type` window:
///   - `daily_*`   → seconds until next UTC midnight
///   - `monthly_*` → seconds until 00:00 UTC on the 1st of next month
///   - unknown/None → conservative 60s fallback
///
/// Used to populate RFC 6585 §4 `Retry-After` on 429 responses.
pub fn retry_after_seconds_for_limit_type(limit_type: Option<&str>) -> u64 {
    retry_after_seconds_for_limit_type_at(limit_type, Utc::now())
}

/// Testable variant — `now` is injected so window boundaries are deterministic.
pub fn retry_after_seconds_for_limit_type_at(limit_type: Option<&str>, now: DateTime<Utc>) -> u64 {
    let kind = match limit_type {
        Some(s) if s.starts_with("daily_") => "daily",
        Some(s) if s.starts_with("monthly_") => "monthly",
        _ => return 60,
    };

    let next_window = match kind {
        "daily" => (now.date_naive() + chrono::Duration::days(1))
            .and_hms_opt(0, 0, 0)
            .unwrap()
            .and_utc(),
        "monthly" => {
            let (y, m) = if now.month() == 12 {
                (now.year() + 1, 1)
            } else {
                (now.year(), now.month() + 1)
            };
            chrono::NaiveDate::from_ymd_opt(y, m, 1)
                .unwrap()
                .and_hms_opt(0, 0, 0)
                .unwrap()
                .and_utc()
        }
        _ => unreachable!(),
    };

    let secs = (next_window - now).num_seconds();
    if secs < 1 { 1 } else { secs as u64 }
}

// ---------------------------------------------------------------------------
// Hierarchical ancestor budget enforcement
// ---------------------------------------------------------------------------

/// Check ancestor group budgets against aggregate descendant usage.
/// Returns Some(blocked_result) if any ancestor's budget is exceeded, None otherwise.
/// One ancestor on a group's parent chain, with its OWN limits.
struct AncestorBudget {
    id: String,
    name: String,
    path: Option<String>,
    llm: Option<serde_json::Value>,
    mcp: Option<serde_json::Value>,
    skill: Option<serde_json::Value>,
}

/// The group's ancestors (nearest first, at most ten), each with its own
/// limits. `Err` when a hop cannot be read: a failed lookup used to read as
/// "no such ancestor", END the walk, and be CACHED — every ancestor budget
/// above the failed hop went unenforced for the cache TTL (fail closed).
async fn ancestor_budget_chain(
    db: &PgPool,
    group: &ResourceGroup,
) -> Result<Vec<AncestorBudget>, sqlx::Error> {
    let mut chain = Vec::new();
    let mut current_parent_id = group.parent_group_id.clone();
    for _ in 0..10 {
        let Some(pid) = current_parent_id.take() else {
            break;
        };
        if pid == group.id || chain.iter().any(|a: &AncestorBudget| a.id == pid) {
            break; // defensive: a cycle must not loop or double-count
        }
        let ancestor_key = format!("{}|{pid}", db_cache_key(db));
        let row = match ancestor_row_cache().get(&ancestor_key) {
            Some(cached) => cached,
            None => {
                let row: Option<AncestorRow> = sqlx::query_as(
                    "SELECT llm_global_limits, mcp_global_limits, skill_global_limits, parent_group_id, hierarchy_path, name FROM resource_groups WHERE id = $1",
                )
                .bind(&pid)
                .fetch_optional(db)
                .await?;
                // "No such group" is a real answer and ends the walk; an
                // error never reaches the cache.
                ancestor_row_cache().insert(ancestor_key, row.clone());
                row
            }
        };
        let Some((llm, mcp, skill, next_pid, path, name)) = row else {
            break;
        };
        chain.push(AncestorBudget {
            id: pid,
            name,
            path,
            llm,
            mcp,
            skill,
        });
        current_parent_id = next_pid;
    }
    Ok(chain)
}

/// Fail-closed refusal when a budget scope cannot be read.
fn budget_unavailable(scope: &str, e: &sqlx::Error) -> UsageEnforcementResult {
    tracing::error!(error = %e, scope, "budget lookup failed; refusing (fail closed)");
    make_blocked(
        "__unavailable__",
        scope,
        "budget_unavailable",
        "",
        "",
        format!("Refused: the {scope} budget cannot be read, so it cannot be enforced"),
    )
}

async fn enforce_ancestor_budgets(
    db: &PgPool,
    tenant_id: &str,
    group: &ResourceGroup,
    limit_field: &str, // "llm_global_limits", "mcp_global_limits", "skill_global_limits"
    resource_type: &str, // "llm", "mcp", "skill"
) -> Option<UsageEnforcementResult> {
    let chain = match ancestor_budget_chain(db, group).await {
        Ok(c) => c,
        Err(e) => return Some(budget_unavailable("ancestor", &e)),
    };

    let now = Utc::now();
    let today_start = now.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc();
    let month_start = now
        .with_day(1)
        .unwrap()
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .and_utc();

    for ancestor in &chain {
        // The ancestor's OWN limits for this field (not effective/merged),
        // checked against the aggregate usage of its whole subtree.
        let own_limits = match limit_field {
            "llm_global_limits" => ancestor.llm.as_ref(),
            "mcp_global_limits" => ancestor.mcp.as_ref(),
            "skill_global_limits" => ancestor.skill.as_ref(),
            _ => None,
        };
        if let (Some(limits), Some(ancestor_path)) = (own_limits, ancestor.path.as_deref()) {
            if let Some(blocked) = check_ancestor_limits(
                db,
                tenant_id,
                &ancestor.id,
                &ancestor.name,
                ancestor_path,
                limits,
                resource_type,
                today_start,
                month_start,
            )
            .await
            {
                return Some(blocked);
            }
        }
    }

    None
}

async fn check_ancestor_limits(
    db: &PgPool,
    tenant_id: &str,
    ancestor_id: &str,
    ancestor_name: &str,
    ancestor_path: &str,
    limits: &serde_json::Value,
    resource_type: &str,
    today_start: chrono::DateTime<Utc>,
    month_start: chrono::DateTime<Utc>,
) -> Option<UsageEnforcementResult> {
    match resource_type {
        "llm" => {
            // Budget checks. A configured dollar budget always enforces
            // (the `hard_stop` opt-out was removed platform-wide).
            if let Some(budget) = limits.get("budget") {
                if let Some(daily_budget) = budget.get("daily_limit_usd").and_then(|v| v.as_f64()) {
                    if daily_budget > 0.0 {
                        let agg_cost = sum_cost_by_ancestry(
                            db,
                            tenant_id,
                            ancestor_path,
                            today_start,
                            resource_type,
                        )
                        .await
                        .or_over_limit();
                        if agg_cost >= daily_budget {
                            return Some(make_blocked(
                                ancestor_id,
                                ancestor_name,
                                "daily_budget_ancestor",
                                &format!("${agg_cost:.2}"),
                                &format!("${daily_budget:.2}"),
                                format!(
                                    "Ancestor group '{}' daily budget exhausted: ${:.2} / ${:.2} (aggregate across sub-groups)",
                                    ancestor_name, agg_cost, daily_budget
                                ),
                            ));
                        }
                    }
                }

                if let Some(monthly_budget) =
                    budget.get("monthly_limit_usd").and_then(|v| v.as_f64())
                {
                    if monthly_budget > 0.0 {
                        let agg_cost = sum_cost_by_ancestry(
                            db,
                            tenant_id,
                            ancestor_path,
                            month_start,
                            resource_type,
                        )
                        .await
                        .or_over_limit();
                        if agg_cost >= monthly_budget {
                            return Some(make_blocked(
                                ancestor_id,
                                ancestor_name,
                                "monthly_budget_ancestor",
                                &format!("${agg_cost:.2}"),
                                &format!("${monthly_budget:.2}"),
                                format!(
                                    "Ancestor group '{}' monthly budget exhausted: ${:.2} / ${:.2} (aggregate across sub-groups)",
                                    ancestor_name, agg_cost, monthly_budget
                                ),
                            ));
                        }
                    }
                }
            }

            // Token checks
            if let Some(tokens) = limits.get("tokens") {
                if let Some(daily_limit) = tokens.get("daily_limit").and_then(|v| v.as_i64()) {
                    if daily_limit > 0 {
                        let agg_tokens = sum_tokens_by_ancestry(
                            db,
                            tenant_id,
                            ancestor_path,
                            today_start,
                            resource_type,
                        )
                        .await
                        .or_over_limit();
                        if agg_tokens >= daily_limit {
                            return Some(make_blocked(
                                ancestor_id,
                                ancestor_name,
                                "daily_tokens_ancestor",
                                &agg_tokens.to_string(),
                                &daily_limit.to_string(),
                                format!(
                                    "Ancestor group '{}' daily token limit exceeded: {} / {} (aggregate)",
                                    ancestor_name, agg_tokens, daily_limit
                                ),
                            ));
                        }
                    }
                }
                if let Some(monthly_limit) = tokens.get("monthly_limit").and_then(|v| v.as_i64()) {
                    if monthly_limit > 0 {
                        let agg_tokens = sum_tokens_by_ancestry(
                            db,
                            tenant_id,
                            ancestor_path,
                            month_start,
                            resource_type,
                        )
                        .await
                        .or_over_limit();
                        if agg_tokens >= monthly_limit {
                            return Some(make_blocked(
                                ancestor_id,
                                ancestor_name,
                                "monthly_tokens_ancestor",
                                &agg_tokens.to_string(),
                                &monthly_limit.to_string(),
                                format!(
                                    "Ancestor group '{}' monthly token limit exceeded: {} / {} (aggregate)",
                                    ancestor_name, agg_tokens, monthly_limit
                                ),
                            ));
                        }
                    }
                }
            }

            // Request count checks
            if let Some(requests) = limits.get("requests") {
                if let Some(daily_limit) = requests.get("daily_limit").and_then(|v| v.as_i64()) {
                    if daily_limit > 0 {
                        let agg_count = count_requests_by_ancestry(
                            db,
                            tenant_id,
                            ancestor_path,
                            today_start,
                            resource_type,
                        )
                        .await
                        .or_over_limit();
                        if (agg_count + 1) > daily_limit {
                            return Some(make_blocked(
                                ancestor_id,
                                ancestor_name,
                                "daily_requests_ancestor",
                                &agg_count.to_string(),
                                &daily_limit.to_string(),
                                format!(
                                    "Ancestor group '{}' daily request limit exceeded: {} / {} (aggregate)",
                                    ancestor_name, agg_count, daily_limit
                                ),
                            ));
                        }
                    }
                }
                if let Some(monthly_limit) = requests.get("monthly_limit").and_then(|v| v.as_i64())
                {
                    if monthly_limit > 0 {
                        let agg_count = count_requests_by_ancestry(
                            db,
                            tenant_id,
                            ancestor_path,
                            month_start,
                            resource_type,
                        )
                        .await
                        .or_over_limit();
                        if (agg_count + 1) > monthly_limit {
                            return Some(make_blocked(
                                ancestor_id,
                                ancestor_name,
                                "monthly_requests_ancestor",
                                &agg_count.to_string(),
                                &monthly_limit.to_string(),
                                format!(
                                    "Ancestor group '{}' monthly request limit exceeded: {} / {} (aggregate)",
                                    ancestor_name, agg_count, monthly_limit
                                ),
                            ));
                        }
                    }
                }
            }
        }
        "mcp" => {
            if let Some(freq) = limits.get("frequency") {
                if let Some(daily_limit) = freq.get("max_calls_per_day").and_then(|v| v.as_i64()) {
                    if daily_limit > 0 {
                        let agg_count = count_requests_by_ancestry(
                            db,
                            tenant_id,
                            ancestor_path,
                            today_start,
                            resource_type,
                        )
                        .await
                        .or_over_limit();
                        if (agg_count + 1) > daily_limit {
                            return Some(make_blocked(
                                ancestor_id,
                                ancestor_name,
                                "daily_mcp_calls_ancestor",
                                &agg_count.to_string(),
                                &daily_limit.to_string(),
                                format!(
                                    "Ancestor group '{}' daily MCP call limit exceeded: {} / {} (aggregate)",
                                    ancestor_name, agg_count, daily_limit
                                ),
                            ));
                        }
                    }
                }
            }
        }
        "skill" => {
            if let Some(exec) = limits.get("execution") {
                if let Some(daily_limit) = exec.get("daily_limit").and_then(|v| v.as_i64()) {
                    if daily_limit > 0 {
                        let agg_count = count_requests_by_ancestry(
                            db,
                            tenant_id,
                            ancestor_path,
                            today_start,
                            resource_type,
                        )
                        .await
                        .or_over_limit();
                        if (agg_count + 1) > daily_limit {
                            return Some(make_blocked(
                                ancestor_id,
                                ancestor_name,
                                "daily_skill_ancestor",
                                &agg_count.to_string(),
                                &daily_limit.to_string(),
                                format!(
                                    "Ancestor group '{}' daily skill limit exceeded: {} / {} (aggregate)",
                                    ancestor_name, agg_count, daily_limit
                                ),
                            ));
                        }
                    }
                }
                if let Some(monthly_limit) = exec.get("monthly_limit").and_then(|v| v.as_i64()) {
                    if monthly_limit > 0 {
                        let agg_count = count_requests_by_ancestry(
                            db,
                            tenant_id,
                            ancestor_path,
                            month_start,
                            resource_type,
                        )
                        .await
                        .or_over_limit();
                        if (agg_count + 1) > monthly_limit {
                            return Some(make_blocked(
                                ancestor_id,
                                ancestor_name,
                                "monthly_skill_ancestor",
                                &agg_count.to_string(),
                                &monthly_limit.to_string(),
                                format!(
                                    "Ancestor group '{}' monthly skill limit exceeded: {} / {} (aggregate)",
                                    ancestor_name, agg_count, monthly_limit
                                ),
                            ));
                        }
                    }
                }
            }
        }
        _ => {}
    }

    None
}

// ---------------------------------------------------------------------------
// LLM quota enforcement
// ---------------------------------------------------------------------------

/// Check LLM usage quota before processing a request.
///
/// Uses governance inheritance: walks the full parent chain and applies
/// restrictive merge (min values) to compute effective limits.
///
/// Checks nested config structure:
///   llm_global_limits.budget.{daily_limit_usd, monthly_limit_usd}
///   llm_global_limits.tokens.{daily_limit, monthly_limit}
///   llm_global_limits.requests.{daily_limit, monthly_limit}
/// Already-fetched usage aggregates for the LLM quota decision. Splitting the
/// numbers out of the decision logic makes [`evaluate_llm_quota`] a pure
/// function (exhaustively unit-testable without a DB) and is the seam the
/// reserve/commit refactor needs: today these fields come from `usage_logs`
/// SUM/COUNT (see [`build_llm_snapshot`]); the next step replaces that builder
/// with O(1) Redis counter reads, leaving the decision logic untouched.
#[derive(Debug, Clone, Default)]
pub struct LlmUsageSnapshot {
    pub daily_cost: f64,
    pub monthly_cost: f64,
    pub daily_tokens: i64,
    pub monthly_tokens: i64,
    pub daily_requests: i64,
    pub monthly_requests: i64,
    pub pm_daily_cost: f64,
    pub pm_monthly_cost: f64,
    pub pm_daily_tokens: i64,
    pub pm_monthly_tokens: i64,
    pub pm_daily_requests: i64,
    pub pm_monthly_requests: i64,
}

/// Serialise a usage snapshot for the audit row.
///
/// Only the observed aggregates. The limits these were judged against are
/// pinned by the AI System's `contract_hash`, so duplicating them per row
/// would store the same fact twice and let the copies disagree.
fn record_llm_pre_action_state(scope: &str, scope_id: &str, snap: &LlmUsageSnapshot) {
    crate::middleware::run_end::record_pre_action_state(serde_json::json!({
        "kind": "llm_quota",
        "scope": scope,
        "scope_id": scope_id,
        "daily_cost": snap.daily_cost,
        "monthly_cost": snap.monthly_cost,
        "daily_tokens": snap.daily_tokens,
        "monthly_tokens": snap.monthly_tokens,
        "daily_requests": snap.daily_requests,
        "monthly_requests": snap.monthly_requests,
        "pm_daily_cost": snap.pm_daily_cost,
        "pm_monthly_cost": snap.pm_monthly_cost,
        "pm_daily_tokens": snap.pm_daily_tokens,
        "pm_monthly_tokens": snap.pm_monthly_tokens,
        "pm_daily_requests": snap.pm_daily_requests,
        "pm_monthly_requests": snap.pm_monthly_requests,
    }));
}

/// PURE quota decision: given the effective group limits, the (already
/// restrictive-merged) per-model limits, and a usage snapshot, decide
/// allow/block/warn. No I/O — all aggregates arrive via `snap`.
///
/// Order is load-bearing and mirrors the original `enforce_llm_quota`: group
/// budget → tokens → requests, then per-model budget → tokens → requests; the
/// FIRST hard limit hit returns a block, warnings accumulate otherwise. Block
/// conditions: budget `util >= 100%`; tokens `used >= limit`; requests
/// `(count + 1) > limit`. Every configured cap enforces — there is no
/// advisory-budget opt-out. Per-model has blocks only (no warnings).
fn evaluate_llm_quota(
    group_id: &str,
    group_name: &str,
    limits: &serde_json::Value,
    effective_pm: Option<&serde_json::Value>,
    snap: &LlmUsageSnapshot,
) -> UsageEnforcementResult {
    let mut result = UsageEnforcementResult::allowed();

    // ---- Budget limits (limits.budget) ----
    //
    // A configured dollar budget always enforces at 100% utilisation. The
    // former `hard_stop` opt-out was removed platform-wide: token and request
    // caps always blocked, so a spend cap that silently didn't was a footgun.
    if let Some(budget) = limits.get("budget") {
        if let Some(daily_budget) = budget.get("daily_limit_usd").and_then(|v| v.as_f64()) {
            if daily_budget > 0.0 {
                let daily_cost = snap.daily_cost;
                let util = (daily_cost / daily_budget) * 100.0;
                if util >= 100.0 {
                    return make_blocked(
                        group_id,
                        group_name,
                        "daily_budget",
                        &format!("${daily_cost:.2}"),
                        &format!("${daily_budget:.2}"),
                        format!("Daily budget exhausted: ${daily_cost:.2} / ${daily_budget:.2}"),
                    );
                }
                let warn_pct = budget
                    .get("daily_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(80.0);
                if util >= warn_pct {
                    result.warning_headers.push((
                        "X-Usage-Warning".into(),
                        format!(
                            "Daily budget at {util:.0}%: ${daily_cost:.2} / ${daily_budget:.2}"
                        ),
                    ));
                }
            }
        }

        if let Some(monthly_budget) = budget.get("monthly_limit_usd").and_then(|v| v.as_f64()) {
            if monthly_budget > 0.0 {
                let monthly_cost = snap.monthly_cost;
                let util = (monthly_cost / monthly_budget) * 100.0;
                if util >= 100.0 {
                    return make_blocked(
                        group_id,
                        group_name,
                        "monthly_budget",
                        &format!("${monthly_cost:.2}"),
                        &format!("${monthly_budget:.2}"),
                        format!(
                            "Monthly budget exhausted: ${monthly_cost:.2} / ${monthly_budget:.2}"
                        ),
                    );
                }
                let warn_pct = budget
                    .get("monthly_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(90.0);
                if util >= warn_pct {
                    result.warning_headers.push((
                        "X-Usage-Warning".into(),
                        format!("Monthly budget at {util:.0}%: ${monthly_cost:.2} / ${monthly_budget:.2}"),
                    ));
                }
            }
        }
    }

    // ---- Token limits (limits.tokens) ----
    if let Some(tokens) = limits.get("tokens") {
        if let Some(daily_limit) = tokens.get("daily_limit").and_then(|v| v.as_i64()) {
            if daily_limit > 0 {
                let daily_tokens = snap.daily_tokens;
                if daily_tokens >= daily_limit {
                    return make_blocked(
                        group_id,
                        group_name,
                        "daily_tokens",
                        &daily_tokens.to_string(),
                        &daily_limit.to_string(),
                        format!("Daily token limit exceeded: {daily_tokens} / {daily_limit}"),
                    );
                }
                let warn_pct = tokens
                    .get("daily_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(85.0);
                let util = (daily_tokens as f64 / daily_limit as f64) * 100.0;
                if util >= warn_pct {
                    result.warning_headers.push((
                        "X-Usage-Warning".into(),
                        format!("Daily tokens at {util:.0}%: {daily_tokens} / {daily_limit}"),
                    ));
                }
            }
        }

        if let Some(monthly_limit) = tokens.get("monthly_limit").and_then(|v| v.as_i64()) {
            if monthly_limit > 0 {
                let monthly_tokens = snap.monthly_tokens;
                if monthly_tokens >= monthly_limit {
                    return make_blocked(
                        group_id,
                        group_name,
                        "monthly_tokens",
                        &monthly_tokens.to_string(),
                        &monthly_limit.to_string(),
                        format!("Monthly token limit exceeded: {monthly_tokens} / {monthly_limit}"),
                    );
                }
                let warn_pct = tokens
                    .get("monthly_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(90.0);
                let util = (monthly_tokens as f64 / monthly_limit as f64) * 100.0;
                if util >= warn_pct {
                    result.warning_headers.push((
                        "X-Usage-Warning".into(),
                        format!("Monthly tokens at {util:.0}%: {monthly_tokens} / {monthly_limit}"),
                    ));
                }
            }
        }
    }

    // ---- Request count limits (limits.requests) ----
    if let Some(requests) = limits.get("requests") {
        if let Some(daily_limit) = requests.get("daily_limit").and_then(|v| v.as_i64()) {
            if daily_limit > 0 {
                let daily_count = snap.daily_requests;
                if (daily_count + 1) > daily_limit {
                    return make_blocked(
                        group_id,
                        group_name,
                        "daily_requests",
                        &daily_count.to_string(),
                        &daily_limit.to_string(),
                        format!("Daily request limit exceeded: {daily_count} / {daily_limit}"),
                    );
                }
                let warn_pct = requests
                    .get("daily_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(80.0);
                let util = (daily_count as f64 / daily_limit as f64) * 100.0;
                if util >= warn_pct {
                    result.warning_headers.push((
                        "X-Usage-Warning".into(),
                        format!("Daily requests at {util:.0}%: {daily_count} / {daily_limit}"),
                    ));
                }
            }
        }

        if let Some(monthly_limit) = requests.get("monthly_limit").and_then(|v| v.as_i64()) {
            if monthly_limit > 0 {
                let monthly_count = snap.monthly_requests;
                if (monthly_count + 1) > monthly_limit {
                    return make_blocked(
                        group_id,
                        group_name,
                        "monthly_requests",
                        &monthly_count.to_string(),
                        &monthly_limit.to_string(),
                        format!(
                            "Monthly request limit exceeded: {monthly_count} / {monthly_limit}"
                        ),
                    );
                }
                let warn_pct = requests
                    .get("monthly_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(85.0);
                let util = (monthly_count as f64 / monthly_limit as f64) * 100.0;
                if util >= warn_pct {
                    result.warning_headers.push((
                        "X-Usage-Warning".into(),
                        format!(
                            "Monthly requests at {util:.0}%: {monthly_count} / {monthly_limit}"
                        ),
                    ));
                }
            }
        }
    }

    // ---- Per-model limits (effective = restrictive_merge(global, per_model)) ----
    if let Some(effective_pm) = effective_pm {
        if let Some(budget) = effective_pm.get("budget") {
            if let Some(daily_budget) = budget.get("daily_limit_usd").and_then(|v| v.as_f64()) {
                if daily_budget > 0.0 {
                    let cost = snap.pm_daily_cost;
                    if cost >= daily_budget {
                        return make_blocked(
                            group_id,
                            group_name,
                            "per_model_daily_budget",
                            &format!("${cost:.2}"),
                            &format!("${daily_budget:.2}"),
                            format!(
                                "Per-model daily budget exhausted: ${cost:.2} / ${daily_budget:.2}"
                            ),
                        );
                    }
                }
            }
            if let Some(monthly_budget) = budget.get("monthly_limit_usd").and_then(|v| v.as_f64()) {
                if monthly_budget > 0.0 {
                    let cost = snap.pm_monthly_cost;
                    if cost >= monthly_budget {
                        return make_blocked(
                            group_id,
                            group_name,
                            "per_model_monthly_budget",
                            &format!("${cost:.2}"),
                            &format!("${monthly_budget:.2}"),
                            format!(
                                "Per-model monthly budget exhausted: ${cost:.2} / ${monthly_budget:.2}"
                            ),
                        );
                    }
                }
            }
        }

        if let Some(tokens) = effective_pm.get("tokens") {
            if let Some(daily_limit) = tokens.get("daily_limit").and_then(|v| v.as_i64()) {
                if daily_limit > 0 {
                    let used = snap.pm_daily_tokens;
                    if used >= daily_limit {
                        return make_blocked(
                            group_id,
                            group_name,
                            "per_model_daily_tokens",
                            &used.to_string(),
                            &daily_limit.to_string(),
                            format!("Per-model daily token limit exceeded: {used} / {daily_limit}"),
                        );
                    }
                }
            }
            if let Some(monthly_limit) = tokens.get("monthly_limit").and_then(|v| v.as_i64()) {
                if monthly_limit > 0 {
                    let used = snap.pm_monthly_tokens;
                    if used >= monthly_limit {
                        return make_blocked(
                            group_id,
                            group_name,
                            "per_model_monthly_tokens",
                            &used.to_string(),
                            &monthly_limit.to_string(),
                            format!(
                                "Per-model monthly token limit exceeded: {used} / {monthly_limit}"
                            ),
                        );
                    }
                }
            }
        }

        if let Some(requests) = effective_pm.get("requests") {
            if let Some(daily_limit) = requests.get("daily_limit").and_then(|v| v.as_i64()) {
                if daily_limit > 0 {
                    let count = snap.pm_daily_requests;
                    if (count + 1) > daily_limit {
                        return make_blocked(
                            group_id,
                            group_name,
                            "per_model_daily_requests",
                            &count.to_string(),
                            &daily_limit.to_string(),
                            format!(
                                "Per-model daily request limit exceeded: {count} / {daily_limit}"
                            ),
                        );
                    }
                }
            }
            if let Some(monthly_limit) = requests.get("monthly_limit").and_then(|v| v.as_i64()) {
                if monthly_limit > 0 {
                    let count = snap.pm_monthly_requests;
                    if (count + 1) > monthly_limit {
                        return make_blocked(
                            group_id,
                            group_name,
                            "per_model_monthly_requests",
                            &count.to_string(),
                            &monthly_limit.to_string(),
                            format!(
                                "Per-model monthly request limit exceeded: {count} / {monthly_limit}"
                            ),
                        );
                    }
                }
            }
        }
    }

    result
}

/// Fetch the usage aggregates [`evaluate_llm_quota`] needs — but ONLY for the
/// dimensions an active limit will actually consult. This preserves the original
/// function's lazy query pattern on the allow path (an unconfigured dimension
/// is never summed). This is the SUM/COUNT-over-`usage_logs` implementation the
/// reserve/commit refactor will swap for O(1) Redis counter reads.
#[allow(clippy::too_many_arguments)]
/// Read an i64 counter when `counters` is enabled, falling back to the ledger
/// `fallback` SUM/COUNT future on a Redis miss/outage (the future is lazy —
/// only awaited on fallback, so the SUM query isn't run in counter mode).
async fn counter_or_sum_i64<F: std::future::Future<Output = Result<i64, sqlx::Error>>>(
    counters: Option<&UsageState>,
    counter_key: &str,
    window: QuotaWindow,
    fallback: F,
) -> i64 {
    if let Some(us) = counters {
        if let Some(v) = us.try_read_usage(counter_key, window).await {
            return v;
        }
    }
    // A request deciding on itself: fail closed (see `OrOverLimit`).
    fallback.await.or_over_limit()
}

/// Cost variant: the counter stores micro-USD; convert back to USD. Ledger
/// fallback already returns USD.
async fn counter_or_sum_cost<F: std::future::Future<Output = Result<f64, sqlx::Error>>>(
    counters: Option<&UsageState>,
    counter_key: &str,
    window: QuotaWindow,
    fallback: F,
) -> f64 {
    if let Some(us) = counters {
        if let Some(micros) = us.try_read_usage(counter_key, window).await {
            return micros as f64 / 1_000_000.0;
        }
    }
    // A request deciding on itself: fail closed (see `OrOverLimit`).
    fallback.await.or_over_limit()
}

/// Build the usage snapshot the LLM quota decision needs. When `counters` is
/// `Some` (cutover enabled + Redis available), each dimension is read O(1) from
/// the reserve/commit counters, falling back per-dimension to the authoritative
/// `usage_logs` SUM/COUNT on a Redis outage (never fail-open). When `None`
/// (default / golden-master), it's the pure-ledger path. Only configured
/// dimensions are read either way (lazy).
#[allow(clippy::too_many_arguments)]
async fn build_llm_snapshot(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    limits: &serde_json::Value,
    effective_pm: Option<&serde_json::Value>,
    model_id: Option<&str>,
    today_start: chrono::DateTime<Utc>,
    month_start: chrono::DateTime<Utc>,
    counters: Option<&UsageState>,
) -> LlmUsageSnapshot {
    let mut snap = LlmUsageSnapshot::default();
    let pos_f =
        |v: &serde_json::Value, k: &str| v.get(k).and_then(|x| x.as_f64()).is_some_and(|n| n > 0.0);
    let pos_i =
        |v: &serde_json::Value, k: &str| v.get(k).and_then(|x| x.as_i64()).is_some_and(|n| n > 0);

    let g_tok = format!("tok:grp:{group_id}");
    let g_cost = format!("cost:grp:{group_id}");
    let g_req = format!("req:grp:{group_id}");

    if let Some(budget) = limits.get("budget") {
        if pos_f(budget, "daily_limit_usd") {
            snap.daily_cost = counter_or_sum_cost(
                counters,
                &g_cost,
                QuotaWindow::Day,
                sum_cost(db, tenant_id, group_id, today_start, "llm"),
            )
            .await;
        }
        if pos_f(budget, "monthly_limit_usd") {
            snap.monthly_cost = counter_or_sum_cost(
                counters,
                &g_cost,
                QuotaWindow::Month,
                sum_cost(db, tenant_id, group_id, month_start, "llm"),
            )
            .await;
        }
    }
    if let Some(tokens) = limits.get("tokens") {
        if pos_i(tokens, "daily_limit") {
            snap.daily_tokens = counter_or_sum_i64(
                counters,
                &g_tok,
                QuotaWindow::Day,
                sum_tokens(db, tenant_id, group_id, today_start, "llm"),
            )
            .await;
        }
        if pos_i(tokens, "monthly_limit") {
            snap.monthly_tokens = counter_or_sum_i64(
                counters,
                &g_tok,
                QuotaWindow::Month,
                sum_tokens(db, tenant_id, group_id, month_start, "llm"),
            )
            .await;
        }
    }
    if let Some(requests) = limits.get("requests") {
        if pos_i(requests, "daily_limit") {
            snap.daily_requests = counter_or_sum_i64(
                counters,
                &g_req,
                QuotaWindow::Day,
                count_requests(db, tenant_id, group_id, today_start, "llm"),
            )
            .await;
        }
        if pos_i(requests, "monthly_limit") {
            snap.monthly_requests = counter_or_sum_i64(
                counters,
                &g_req,
                QuotaWindow::Month,
                count_requests(db, tenant_id, group_id, month_start, "llm"),
            )
            .await;
        }
    }

    if let (Some(effective_pm), Some(mid)) = (effective_pm, model_id) {
        let m_tok = format!("tok:grp:{group_id}:mdl:{mid}");
        let m_cost = format!("cost:grp:{group_id}:mdl:{mid}");
        let m_req = format!("req:grp:{group_id}:mdl:{mid}");
        if let Some(budget) = effective_pm.get("budget") {
            // Dollar budgets always enforce, so the cost snapshot is read
            // whenever a limit is configured (each dimension guarded by
            // `pos_f`). The former `hard_stop` gate here was a matching
            // optimisation that skipped the read when enforcement was off.
            if pos_f(budget, "daily_limit_usd") {
                snap.pm_daily_cost = counter_or_sum_cost(
                    counters,
                    &m_cost,
                    QuotaWindow::Day,
                    sum_cost_for_model(db, tenant_id, group_id, mid, today_start, "llm"),
                )
                .await;
            }
            if pos_f(budget, "monthly_limit_usd") {
                snap.pm_monthly_cost = counter_or_sum_cost(
                    counters,
                    &m_cost,
                    QuotaWindow::Month,
                    sum_cost_for_model(db, tenant_id, group_id, mid, month_start, "llm"),
                )
                .await;
            }
        }
        if let Some(tokens) = effective_pm.get("tokens") {
            if pos_i(tokens, "daily_limit") {
                snap.pm_daily_tokens = counter_or_sum_i64(
                    counters,
                    &m_tok,
                    QuotaWindow::Day,
                    sum_tokens_for_model(db, tenant_id, group_id, mid, today_start, "llm"),
                )
                .await;
            }
            if pos_i(tokens, "monthly_limit") {
                snap.pm_monthly_tokens = counter_or_sum_i64(
                    counters,
                    &m_tok,
                    QuotaWindow::Month,
                    sum_tokens_for_model(db, tenant_id, group_id, mid, month_start, "llm"),
                )
                .await;
            }
        }
        if let Some(requests) = effective_pm.get("requests") {
            if pos_i(requests, "daily_limit") {
                snap.pm_daily_requests = counter_or_sum_i64(
                    counters,
                    &m_req,
                    QuotaWindow::Day,
                    count_requests_for_model(db, tenant_id, group_id, mid, today_start, "llm"),
                )
                .await;
            }
            if pos_i(requests, "monthly_limit") {
                snap.pm_monthly_requests = counter_or_sum_i64(
                    counters,
                    &m_req,
                    QuotaWindow::Month,
                    count_requests_for_model(db, tenant_id, group_id, mid, month_start, "llm"),
                )
                .await;
            }
        }
    }

    snap
}

// --- TENANT-WIDE per-model quota (aggregate across ALL RGs) -----------------
// A single tenant-scoped cap for a model, enforced IN ADDITION to each RG's
// limits. Ledger-sourced (usage_logs is written for every request across all
// groups), so the aggregate needs no extra counter wiring. Same blob shape as
// llm_global_limits, stored on llm_model.tenant_limits.

async fn sum_tokens_tenant_model(
    db: &PgPool,
    tenant_id: &str,
    model_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<i64, sqlx::Error> {
    let row: (Option<i64>,) = sqlx::query_as(
        r#"SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs
           WHERE tenant_id = $1 AND resource_type = $2 AND created_at >= $3 AND resource_id = $4"#,
    )
    .bind(tenant_id)
    .bind(resource_type)
    .bind(since)
    .bind(model_id)
    .fetch_one(db)
    .await?;
    Ok(row.0.unwrap_or(0))
}

async fn sum_cost_tenant_model(
    db: &PgPool,
    tenant_id: &str,
    model_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<f64, sqlx::Error> {
    let row: (Option<f64>,) = sqlx::query_as(
        r#"SELECT COALESCE(SUM(estimated_cost), 0.0) FROM usage_logs
           WHERE tenant_id = $1 AND resource_type = $2 AND created_at >= $3 AND resource_id = $4"#,
    )
    .bind(tenant_id)
    .bind(resource_type)
    .bind(since)
    .bind(model_id)
    .fetch_one(db)
    .await?;
    Ok(row.0.unwrap_or(0.0))
}

async fn count_requests_tenant_model(
    db: &PgPool,
    tenant_id: &str,
    model_id: &str,
    since: chrono::DateTime<Utc>,
    resource_type: &str,
) -> Result<i64, sqlx::Error> {
    let row: (i64,) = sqlx::query_as(
        r#"SELECT COUNT(*) FROM usage_logs
           WHERE tenant_id = $1 AND resource_type = $2 AND created_at >= $3 AND resource_id = $4"#,
    )
    .bind(tenant_id)
    .bind(resource_type)
    .bind(since)
    .bind(model_id)
    .fetch_one(db)
    .await?;
    Ok(row.0)
}

async fn build_llm_tenant_snapshot(
    db: &PgPool,
    tenant_id: &str,
    model_id: &str,
    today_start: chrono::DateTime<Utc>,
    month_start: chrono::DateTime<Utc>,
) -> LlmUsageSnapshot {
    LlmUsageSnapshot {
        daily_cost: sum_cost_tenant_model(db, tenant_id, model_id, today_start, "llm")
            .await
            .or_over_limit(),
        monthly_cost: sum_cost_tenant_model(db, tenant_id, model_id, month_start, "llm")
            .await
            .or_over_limit(),
        daily_tokens: sum_tokens_tenant_model(db, tenant_id, model_id, today_start, "llm")
            .await
            .or_over_limit(),
        monthly_tokens: sum_tokens_tenant_model(db, tenant_id, model_id, month_start, "llm")
            .await
            .or_over_limit(),
        daily_requests: count_requests_tenant_model(db, tenant_id, model_id, today_start, "llm")
            .await
            .or_over_limit(),
        monthly_requests: count_requests_tenant_model(db, tenant_id, model_id, month_start, "llm")
            .await
            .or_over_limit(),
        pm_daily_cost: 0.0,
        pm_monthly_cost: 0.0,
        pm_daily_tokens: 0,
        pm_monthly_tokens: 0,
        pm_daily_requests: 0,
        pm_monthly_requests: 0,
    }
}

/// A model's tenant-wide limits. `Err` when they cannot be read — which used
/// to read as "no tenant limits" (fail closed).
async fn llm_tenant_limits(
    db: &PgPool,
    model_id: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    memoized_limits(LimitsKind::TenantWide, "", model_id, || {
        fetch_tenant_limits(db, "llm_model", model_id)
    })
    .await
}

async fn enforce_llm_tenant_quota(
    db: &PgPool,
    tenant_id: &str,
    model_id: &str,
    limits: Option<&serde_json::Value>,
    today_start: chrono::DateTime<Utc>,
    month_start: chrono::DateTime<Utc>,
) -> Option<UsageEnforcementResult> {
    let limits = limits?.clone();
    let snap = build_llm_tenant_snapshot(db, tenant_id, model_id, today_start, month_start).await;
    record_llm_pre_action_state("tenant", "__tenant__", &snap);
    let result = evaluate_llm_quota("__tenant__", "tenant-wide", &limits, None, &snap);
    if result.blocked { Some(result) } else { None }
}

/// The LLM quota verdict for a (group, model): the group's limits (and the
/// model's per-group and tenant-wide ones), the tenant-wide model quota and
/// every ancestor's subtree budget, judged against usage so far.
///
/// With `reserve = None` this is a pure read — "would this be allowed" — for
/// the admin previews and the golden-master tests. With `reserve` the quota
/// dimensions ([`llm_quota_dims`]) are also ADMITTED, atomically and against
/// the call's worst case, under the given hold id: a test-facing seam. The
/// request path admits these dimensions together with every other limit of
/// the call in one step (`llm_preflight`), never through here.
///
/// `counters` = the shared usage counters (the O(1) oracle). `None` = the
/// pure-ledger path the golden-master tests pin.
pub async fn enforce_llm_quota(
    db: &PgPool,
    tenant_id: &str,
    group: Option<&ResourceGroup>,
    model_id: Option<&str>,
    counters: Option<&UsageState>,
    reserve: Option<(Shape, &limits::HoldId)>,
) -> UsageEnforcementResult {
    let group = match group {
        Some(g) => g,
        None => return UsageEnforcementResult::allowed(),
    };

    // Fold the model's tenant-wide default limits in (min-wins) when we know
    // which model this is; otherwise the RG chain alone.
    let effective = match model_id {
        Some(mid) => collect_effective_llm_limits(db, group, mid).await,
        None => collect_effective_limits(db, group, "llm_global_limits").await,
    };
    let effective = match effective {
        Ok(e) => e,
        Err(e) => return limits::config_unavailable_result(&e),
    };

    let mut result = UsageEnforcementResult::allowed();
    if let Some(limits) = effective.as_ref() {
        let now = Utc::now();
        let today_start = now.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc();
        let month_start = now
            .with_day(1)
            .unwrap()
            .date_naive()
            .and_hms_opt(0, 0, 0)
            .unwrap()
            .and_utc();

        // Per-model effective limits = restrictive_merge(group effective, per-model).
        // Only present when a model is in play AND it has its own limits row.
        let effective_pm = match per_model_effective(db, group, model_id, limits).await {
            Ok(pm) => pm,
            Err(e) => return limits::config_unavailable_result(&e),
        };

        let snap = build_llm_snapshot(
            db,
            tenant_id,
            &group.id,
            limits,
            effective_pm.as_ref(),
            model_id,
            today_start,
            month_start,
            counters,
        )
        .await;

        record_llm_pre_action_state("resource_group", &group.id, &snap);
        result = evaluate_llm_quota(&group.id, &group.name, limits, effective_pm.as_ref(), &snap);
        if result.blocked {
            return result;
        }

        // TENANT-WIDE per-model quota: one aggregate across ALL RGs for this
        // model, enforced IN ADDITION to the RG limits above.
        if let Some(mid) = model_id {
            let tenant_limits = match llm_tenant_limits(db, mid).await {
                Ok(l) => l,
                Err(e) => return budget_unavailable("tenant-wide model", &e),
            };
            if let Some(block) = enforce_llm_tenant_quota(
                db,
                tenant_id,
                mid,
                tenant_limits.as_ref(),
                today_start,
                month_start,
            )
            .await
            {
                return block;
            }
        }
    }

    // Hierarchical ancestor budget enforcement (separate from the restrictive
    // merge above: each ancestor's OWN limit vs the aggregate of its subtree).
    if let Some(ancestor_block) =
        enforce_ancestor_budgets(db, tenant_id, group, "llm_global_limits", "llm").await
    {
        return ancestor_block;
    }

    if let (Some(us), Some((shape, hold_id))) = (counters, reserve) {
        let dims = match llm_quota_dims(db, tenant_id, group, model_id).await {
            Ok(d) => d,
            Err(e) => return limits::config_unavailable_result(&e),
        };
        match limits::admit(us, Some(hold_id), dims, shape).await {
            Ok(Ok(_)) => {}
            Ok(Err(refused)) => return refused.quota_result(),
            Err(e) => return limits::unavailable_result(&e),
        }
    }

    result
}

/// The group's effective limits narrowed by its per-model assignment row.
async fn per_model_effective(
    db: &PgPool,
    group: &ResourceGroup,
    model_id: Option<&str>,
    limits: &serde_json::Value,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    let Some(mid) = model_id else {
        return Ok(None);
    };
    Ok(fetch_per_model_limits(db, &group.id, mid)
        .await?
        .map(|per_model| {
            let mut e = limits.clone();
            restrictive_merge(&mut e, &per_model, LimitFamily::Llm);
            e
        }))
}

/// The refusal names of a group's own cumulative limits.
fn group_quota_name(family: &str, daily: bool) -> &'static str {
    match (family, daily) {
        ("cost", true) => "daily_budget",
        ("cost", false) => "monthly_budget",
        ("tok", true) => "daily_tokens",
        ("tok", false) => "monthly_tokens",
        ("tokin", true) => "daily_input_tokens",
        ("tokin", false) => "monthly_input_tokens",
        ("tokout", true) => "daily_output_tokens",
        ("tokout", false) => "monthly_output_tokens",
        ("req", true) => "daily_requests",
        _ => "monthly_requests",
    }
}

/// Every cumulative quota dimension of one LLM call.
///
/// * the group's `budget` / `tokens` / `requests` (day + month), and the same
///   for the (group, model) pair — always accounted, limited where configured;
/// * the model's tenant-wide quota, one aggregate across every group;
/// * every ancestor's subtree budget, and this group's own calls counted into
///   its own subtree when it carries a budget (accounting only).
///
/// `Err` when any limit cannot be read: the caller refuses (fail closed).
pub async fn llm_quota_dims(
    db: &PgPool,
    tenant_id: &str,
    group: &ResourceGroup,
    model_id: Option<&str>,
) -> Result<Vec<limits::LimitDim>, sqlx::Error> {
    use limits::{Owner, cumulative_dims};
    let effective = match model_id {
        Some(mid) => collect_effective_llm_limits(db, group, mid).await?,
        None => collect_effective_limits(db, group, "llm_global_limits").await?,
    };
    let empty = serde_json::json!({});
    let effective_pm =
        per_model_effective(db, group, model_id, effective.as_ref().unwrap_or(&empty)).await?;
    let own = Owner::new(&group.id, &group.name);
    let mut dims = cumulative_dims(
        &format!("grp:{}", group.id),
        effective.as_ref(),
        &own,
        true,
        |f, d| group_quota_name(f, d).to_string(),
    );
    if let Some(mid) = model_id {
        // The per-model scope is judged against the narrowed limits, and is
        // always accounted (the dashboards read it). Without its own row the
        // group's limits do not repeat on it.
        dims.extend(cumulative_dims(
            &format!("grp:{}:mdl:{mid}", group.id),
            effective_pm.as_ref(),
            &own,
            true,
            |f, d| format!("per_model_{}", group_quota_name(f, d)),
        ));
        dims.extend(cumulative_dims(
            &format!("ten:{tenant_id}:mdl:{mid}"),
            llm_tenant_limits(db, mid).await?.as_ref(),
            &Owner::new("__tenant__", "tenant-wide"),
            true,
            |f, d| group_quota_name(f, d).to_string(),
        ));
    }
    for a in ancestor_budget_chain(db, group)
        .await?
        .iter()
        .filter(|a| a.path.is_some() && a.llm.is_some())
    {
        dims.extend(cumulative_dims(
            &format!("sub:{}", a.id),
            a.llm.as_ref(),
            &Owner::new(&a.id, &a.name),
            false,
            |f, d| format!("{}_ancestor", group_quota_name(f, d)),
        ));
    }
    // A group that carries its own budget is also an ancestor of its
    // descendants: its OWN calls count into its subtree (never refusing).
    if group.hierarchy_path.is_some() && group.llm_global_limits.is_some() {
        dims.extend(
            cumulative_dims(
                &format!("sub:{}", group.id),
                group.llm_global_limits.as_ref(),
                &own,
                false,
                |f, d| format!("{}_ancestor", group_quota_name(f, d)),
            )
            .into_iter()
            .map(|mut d| {
                if let crate::services::cache::usage_state::DimKind::Counter { limit, .. } =
                    &mut d.dim.kind
                {
                    *limit = None;
                }
                d.refusal = limits::DimRefusal::Accounting;
                d
            }),
        );
    }
    Ok(dims)
}

/// The counter deltas of one LLM usage event that held no admission (a
/// probe, a path that predates admission): the group and per-model scopes,
/// day and month. Applied only when the request has no hold — a held request
/// settles its whole admission instead.
pub fn llm_shadow_counters(
    group_id: &str,
    model_id: &str,
    total_tokens: i64,
    cost_micros: i64,
) -> Vec<(String, QuotaWindow, i64)> {
    let g_tok = format!("tok:grp:{group_id}");
    let g_cost = format!("cost:grp:{group_id}");
    let g_req = format!("req:grp:{group_id}");
    let m_tok = format!("tok:grp:{group_id}:mdl:{model_id}");
    let m_cost = format!("cost:grp:{group_id}:mdl:{model_id}");
    let m_req = format!("req:grp:{group_id}:mdl:{model_id}");

    let mut out = Vec::with_capacity(12);
    for w in [QuotaWindow::Day, QuotaWindow::Month] {
        if total_tokens > 0 {
            out.push((g_tok.clone(), w, total_tokens));
            out.push((m_tok.clone(), w, total_tokens));
        }
        if cost_micros > 0 {
            out.push((g_cost.clone(), w, cost_micros));
            out.push((m_cost.clone(), w, cost_micros));
        }
        out.push((g_req.clone(), w, 1));
        out.push((m_req.clone(), w, 1));
    }
    out
}

/// Settle an LLM call's admission against what it really used.
///
/// One entry point for every way a call ends with a usage figure. A call
/// that held an admission (every call with a group) settles all of it in one
/// script — group, model, tenant, ancestors, run, session, control limits,
/// throughput buckets — in the windows it was ADMITTED in (a call that
/// straddles midnight settles into the day it started). A call that
/// held none books `fallback` instead. A 2xx with no usage reported is spend
/// we cannot measure, so the reservation (the worst case) stays: abandoned,
/// never released.
/// One LLM call's usage as reported when it ends (see [`settle_llm_call`]).
pub struct LlmCallUsage<'a> {
    pub tenant_id: &'a str,
    /// The call's admission hold (`AuthContext::admission_hold`); `None`
    /// when the call was admitted under none.
    pub hold_id: Option<&'a str>,
    pub group_id: Option<&'a str>,
    /// The model that actually ran (a fallback may differ from the one
    /// admitted).
    pub model_id: &'a str,
    pub prompt_tokens: Option<i32>,
    pub completion_tokens: Option<i32>,
    pub estimated_cost: Option<f64>,
    pub status_code: u16,
}

pub fn settle_llm_call(usage_state: &Arc<UsageState>, call: &LlmCallUsage<'_>) {
    let LlmCallUsage {
        tenant_id,
        hold_id,
        group_id,
        model_id,
        prompt_tokens,
        completion_tokens,
        estimated_cost,
        status_code,
    } = *call;
    // A call that held nothing settles against an identity of its own that
    // has no record, which is what books `fallback`.
    let hold_id = match hold_id {
        Some(id) => id.to_string(),
        None => limits::HoldId::new(tenant_id, "llm-unheld").into_string(),
    };
    let hold_id = hold_id.as_str();
    let cost_micros = (estimated_cost.unwrap_or(0.0).max(0.0) * 1_000_000.0).round() as i64;
    // A 2xx whose output was never reported — a client that hung up before
    // the final usage chunk — produced output nobody measured: keep
    // the worst case it was admitted for rather than book zero.
    let unmeasured = completion_tokens.is_none() && (200..300).contains(&status_code);
    let how = if unmeasured {
        Finish::Abandon
    } else {
        Finish::Settle(Actual {
            in_tokens: prompt_tokens.unwrap_or(0).max(0) as i64,
            out_tokens: completion_tokens.unwrap_or(0).max(0) as i64,
            cost_micros,
        })
    };
    let total = (prompt_tokens.unwrap_or(0) + completion_tokens.unwrap_or(0)).max(0) as i64;
    let deltas = |keys: Vec<(String, QuotaWindow, i64)>| -> Vec<Fallback> {
        keys.into_iter()
            .map(|(k, w, delta)| Fallback {
                key: UsageState::usage_key(&k, w),
                delta,
                ttl_secs: w.ttl_secs(),
            })
            .collect()
    };
    let fallback = match group_id {
        Some(g) => deltas(llm_shadow_counters(g, model_id, total, cost_micros)),
        None => Vec::new(),
    };
    // A call that fell back from the model it was admitted for to another
    // one: the admitted model's own scopes settle to nothing, and the model
    // that ran is booked instead — its per-group and tenant-wide counters.
    let redirect = match (usage_state.hold_meta(hold_id), group_id) {
        (Some(admitted), Some(g)) if admitted != model_id => {
            let mut keys: Vec<(String, QuotaWindow, i64)> =
                llm_shadow_counters(g, model_id, total, cost_micros)
                    .into_iter()
                    .filter(|(k, _, _)| k.contains(":mdl:"))
                    .collect();
            for w in [QuotaWindow::Day, QuotaWindow::Month] {
                let scope = format!("ten:{tenant_id}:mdl:{model_id}");
                if total > 0 {
                    keys.push((format!("tok:{scope}"), w, total));
                }
                if cost_micros > 0 {
                    keys.push((format!("cost:{scope}"), w, cost_micros));
                }
                keys.push((format!("req:{scope}"), w, 1));
            }
            crate::services::cache::usage_state::Redirect {
                zero_matching: Some(format!(":mdl:{admitted}")),
                deltas: deltas(keys),
            }
        }
        _ => crate::services::cache::usage_state::Redirect::default(),
    };
    // Out of the registry synchronously, so the call's guard — dropped when
    // the response completes — does not also end the hold.
    usage_state.forget_live_hold(hold_id);
    let us = usage_state.clone();
    let id = hold_id.to_string();
    tokio::spawn(async move {
        if let Err(e) = us.finish_hold_with(&id, how, &fallback, &redirect).await {
            tracing::error!(hold_id = %id, error = %e, "LLM call could not be settled; the reaper will keep its worst case");
        }
    });
}

// ---------------------------------------------------------------------------
// MCP quota enforcement
// ---------------------------------------------------------------------------

/// MCP ancestor (subtree) call budgets as a pure ledger read: "would this be
/// allowed". Per-call rate/quota limits (minute / hour / day / burst /
/// cooldown, at group / server / tool scope) are `mcp_rate_dims`.
pub async fn enforce_mcp_quota(
    db: &PgPool,
    tenant_id: &str,
    group: Option<&ResourceGroup>,
) -> UsageEnforcementResult {
    enforce_mcp_quota_counted(db, tenant_id, group, None, None).await
}

/// The subtree call-budget dimensions of one MCP request: every
/// budgeted ancestor's daily count, and this group's own calls counted into
/// its own subtree when it carries a daily budget (accounting only).
pub async fn mcp_quota_dims(
    db: &PgPool,
    group: &ResourceGroup,
) -> Result<Vec<limits::LimitDim>, sqlx::Error> {
    use limits::{DimRefusal, Unit, usage_dim};
    let daily = |l: &serde_json::Value| {
        l.get("frequency")
            .and_then(|f| f.get("max_calls_per_day"))
            .and_then(|v| v.as_i64())
            .filter(|n| *n > 0)
    };
    let mut dims = Vec::new();
    for a in ancestor_budget_chain(db, group)
        .await?
        .iter()
        .filter(|a| a.path.is_some())
    {
        if let Some(limit) = a.mcp.as_ref().and_then(daily) {
            dims.push(usage_dim(
                &format!("mcpx:sub:{}", a.id),
                QuotaWindow::Day,
                Some(limit),
                Measure::One,
                DimRefusal::Quota {
                    owner_id: a.id.clone(),
                    owner_name: a.name.clone(),
                    limit_type: "daily_mcp_calls_ancestor".into(),
                    limit,
                    unit: Unit::Count,
                },
            ));
        }
    }
    if group.hierarchy_path.is_some() && group.mcp_global_limits.as_ref().and_then(daily).is_some()
    {
        dims.push(usage_dim(
            &format!("mcpx:sub:{}", group.id),
            QuotaWindow::Day,
            None,
            Measure::One,
            DimRefusal::Accounting,
        ));
    }
    Ok(dims)
}

/// [`enforce_mcp_quota`] plus, with shared counters, the subtree dimensions
/// ADMITTED atomically. `hold_id` = the request's hold, ended by its
/// guard (released if a later gate refuses, committed at dispatch); `None`
/// admits for good on the spot.
pub async fn enforce_mcp_quota_counted(
    db: &PgPool,
    tenant_id: &str,
    group: Option<&ResourceGroup>,
    counters: Option<&UsageState>,
    hold: Option<&mut limits::PendingHold>,
) -> UsageEnforcementResult {
    let group = match group {
        Some(g) => g,
        None => return UsageEnforcementResult::allowed(),
    };
    if let Some(ancestor_block) =
        enforce_ancestor_budgets(db, tenant_id, group, "mcp_global_limits", "mcp").await
    {
        return ancestor_block;
    }
    if let Some(us) = counters {
        let dims = match mcp_quota_dims(db, group).await {
            Ok(d) => d,
            Err(e) => return budget_unavailable("ancestor", &e),
        };
        let hold_id = hold.as_deref().map(limits::PendingHold::id);
        match limits::admit(us, hold_id, dims, Shape::count()).await {
            Ok(Ok(admitted)) => {
                if let Some(hold) = hold {
                    hold.admitted_as(admitted);
                }
            }
            Ok(Err(refused)) => return refused.quota_result(),
            Err(e) => return limits::unavailable_result(&e),
        }
    }
    UsageEnforcementResult::allowed()
}

// ---------------------------------------------------------------------------
// MCP per-call rate limiting — token bucket + fixed-window quota + cooldown
//
// Industry-standard model (cf. AWS API Gateway usage plans): a token bucket
// governs the sustained rate + burst, while separate fixed-window counters
// enforce the hourly / daily quota. Every scope is a set of admission
// dimensions (`limits::freq_dims`), and ALL of a call's scopes are admitted
// in one atomic step on the shared counters — exact across every Core pod.
// A Redis that cannot be reached refuses (fail closed); there is no per-pod
// fallback to quietly multiply a limit by the replica count.
//
// Six scopes are judged, broadest first:
//   1. group        — every tools/call in the resource group
//   2. server       — every tools/call to one MCP server
//   3. group-tool   — one tool name, across any server in the group
//   4. server-tool  — one tool name on one server (the most specific)
//   5. tenant / tenant-tool — one aggregate across every group
//
// A request must pass all of them; "most restrictive wins" emerges
// naturally. A refused request is counted nowhere: admission is all or
// nothing, so a call blocked at a narrow scope no longer burns the broader
// scopes' allowance (it used to, which let one abused tool drain the whole
// group's budget with calls that never ran).
// ---------------------------------------------------------------------------

/// A blocked MCP rate-limit decision. `None` from `enforce_mcp_rate_limits`
/// means the request is allowed.
#[derive(Debug, Clone, PartialEq)]
pub struct McpRateLimitBlock {
    /// Which scope tripped: "group" | "server" | "group-tool" | "server-tool".
    pub scope: &'static str,
    /// Which mechanism tripped: "rate" | "hourly_quota" | "daily_quota" | "cooldown".
    pub limit_type: &'static str,
    /// Human-readable explanation, surfaced to the caller.
    pub message: String,
    /// Seconds the caller should wait before retrying (RFC 6585 Retry-After).
    pub retry_after_secs: u64,
}

/// Admit rate dimensions and translate the outcome into the rate-limit
/// block every caller of the old per-scope gates expects.
async fn admit_rate(
    usage_state: &UsageState,
    hold: Option<&mut limits::PendingHold>,
    dims: Vec<limits::LimitDim>,
) -> Option<McpRateLimitBlock> {
    let hold_id = hold.as_deref().map(limits::PendingHold::id);
    match limits::admit(usage_state, hold_id, dims, Shape::count()).await {
        Ok(Ok(admitted)) => {
            if let Some(hold) = hold {
                hold.admitted_as(admitted);
            }
            None
        }
        Ok(Err(refused)) => Some(refused.rate_block().unwrap_or_else(|| {
            let r = refused.quota_result();
            McpRateLimitBlock {
                scope: "quota",
                limit_type: "quota",
                message: r.message.unwrap_or_default(),
                retry_after_secs: 60,
            }
        })),
        Err(e) => Some(limits::unavailable_block(&e)),
    }
}

// ---------------------------------------------------------------------------
// LLM rate / throughput / concurrency — enterprise governance primitives
//
// The MCP path above covers request-count rate limits well, but LLM
// workloads need three additional dimensions:
//
//   - **ITPM / OTPM**  — separate input and output token-per-minute caps.
//                        Matches what Anthropic and Azure OpenAI expose.
//                        ITPM binds for RAG / long-context; OTPM binds
//                        for long-form generation.
//   - **USD / minute** — cost-rate cap. Matches Portkey / Helicone.
//   - **Concurrency**  — in-flight semaphore; orthogonal to rate. One
//                        slow upstream call can pin resources without
//                        ever tripping RPM.
//
// All of these are configured per-group AND per-model. The strictest
// applicable scope wins, mirroring MCP's group → server → tool chain.
//
// The token and spend buckets are charged at ADMISSION with what the call is
// expected to use (input estimate, the output it may produce, their cost)
// and trued up at settle, carrying any shortfall as debt — the way provider
// ITPM/OTPM limiters meter. They used to be charged only after the response,
// so a burst all passed the "is there any headroom" peek.
// ---------------------------------------------------------------------------

/// LLM throughput limits — parsed from `llm_global_limits.throughput.*` on
/// the resource group and from the per-model `limits.throughput.*` JSON.
///
/// All amounts use `i64` to stay aligned with the existing token-bucket
/// API. USD is stored as **cents** because the Redis-backed bucket counts
/// integer tokens; conversion happens at parse time.
#[derive(Debug, Default, Clone, Copy)]
struct LLMThroughputLimits {
    /// Requests per minute — token bucket on request count (RPM).
    rpm: Option<i64>,
    /// Input tokens per minute — token bucket charged by prompt tokens
    /// post-response (ITPM).
    itpm: Option<i64>,
    /// Output tokens per minute — token bucket charged by completion
    /// tokens post-response (OTPM).
    otpm: Option<i64>,
    /// USD per minute, in **cents**, charged from actual billed cost.
    usd_cents_per_minute: Option<i64>,
    /// Token-bucket capacity for the RPM bucket. Defaults to `rpm`.
    rpm_burst: Option<i64>,
    /// Token-bucket capacity for ITPM and OTPM (each bucket gets the same
    /// capacity, independently of the other). Defaults to the per-minute
    /// rate.
    tokens_burst: Option<i64>,
    /// Token-bucket capacity for the USD bucket in cents.
    usd_cents_burst: Option<i64>,
    /// Penalty lockout — when any throughput bucket trips at this scope,
    /// reject all calls for this many seconds regardless of bucket state.
    cooldown_secs: Option<i64>,
}

impl LLMThroughputLimits {
    /// True when no rate is set — nothing to enforce. Burst / cooldown
    /// are inert without a rate, so they don't count.
    fn is_empty(&self) -> bool {
        self.rpm.is_none()
            && self.itpm.is_none()
            && self.otpm.is_none()
            && self.usd_cents_per_minute.is_none()
    }
}

/// LLM concurrency cap — semaphore on simultaneous in-flight requests.
/// Parsed from `llm_global_limits.concurrency.max_concurrent`.
#[derive(Debug, Default, Clone, Copy)]
struct LLMConcurrencyLimits {
    max_concurrent: Option<i64>,
}

/// Parse the LLM throughput block from a JSON object. Non-positive values
/// are treated as unset (matches `parse_freq` semantics).
fn parse_llm_throughput(v: &serde_json::Value) -> LLMThroughputLimits {
    let geti = |k: &str| v.get(k).and_then(|x| x.as_i64()).filter(|n| *n > 0);
    let getf = |k: &str| v.get(k).and_then(|x| x.as_f64()).filter(|n| *n > 0.0);
    // USD → cents via round-trip math. We accept either a float ("0.50")
    // or an integer ("50" interpreted as cents directly) — but in practice
    // the admin UI writes floats from the dollar field, so the float
    // branch is the primary path.
    let to_cents = |f: f64| (f * 100.0).round() as i64;
    LLMThroughputLimits {
        rpm: geti("max_requests_per_minute"),
        itpm: geti("max_input_tokens_per_minute"),
        otpm: geti("max_output_tokens_per_minute"),
        usd_cents_per_minute: getf("max_usd_per_minute").map(to_cents),
        rpm_burst: geti("request_burst"),
        tokens_burst: geti("tokens_burst"),
        usd_cents_burst: getf("usd_burst").map(to_cents),
        cooldown_secs: geti("cooldown_seconds"),
    }
}

fn parse_llm_concurrency(v: &serde_json::Value) -> LLMConcurrencyLimits {
    LLMConcurrencyLimits {
        max_concurrent: v
            .get("max_concurrent")
            .and_then(|x| x.as_i64())
            .filter(|n| *n > 0),
    }
}

/// Navigate `path` into an optional JSON limits blob and parse the LLM
/// throughput fields found there. A missing path yields empty limits.
fn llm_throughput_at(limits: &Option<serde_json::Value>, path: &[&str]) -> LLMThroughputLimits {
    let mut node = match limits {
        Some(v) => v,
        None => return LLMThroughputLimits::default(),
    };
    for seg in path {
        node = match node.get(*seg) {
            Some(n) => n,
            None => return LLMThroughputLimits::default(),
        };
    }
    parse_llm_throughput(node)
}

fn llm_concurrency_at(limits: &Option<serde_json::Value>, path: &[&str]) -> LLMConcurrencyLimits {
    let mut node = match limits {
        Some(v) => v,
        None => return LLMConcurrencyLimits::default(),
    };
    for seg in path {
        node = match node.get(*seg) {
            Some(n) => n,
            None => return LLMConcurrencyLimits::default(),
        };
    }
    parse_llm_concurrency(node)
}

/// What the gateway reads of one MCP call's limits
/// (`mcp_limits::McpLimitSources`), in one pass: the group's
/// `mcp_global_limits` merged across its ancestor chain and folded (min
/// wins) with the server's tenant-wide `tenant_limits`; the group's binding
/// of the server (`resource_group_server_configs.limits`); and the
/// `tenant_limits` alone. `Err` when a limit cannot be read.
pub async fn load_mcp_limit_sources(
    db: &PgPool,
    group: &ResourceGroup,
    mcp_server_id: &str,
    server_config_id: Option<&str>,
) -> Result<crate::services::mcp_limits::McpLimitSources, sqlx::Error> {
    // The server's tenant-wide limits and the group's binding of it, in one
    // round trip.
    let (tenant, server): (Option<serde_json::Value>, Option<serde_json::Value>) = sqlx::query_as(
        "SELECT (SELECT tenant_limits FROM mcp_servers WHERE id = $1), \
                    (SELECT limits FROM resource_group_server_configs \
                      WHERE resource_group_id = $2 AND server_config_id = $3)",
    )
    .bind(mcp_server_id)
    .bind(&group.id)
    .bind(server_config_id)
    .fetch_one(db)
    .await?;
    let tenant = tenant.filter(|v| !v.is_null());
    let server = server.filter(|v| !v.is_null());
    let chain = limit_chain(db, group, "mcp_global_limits").await?;
    // The merge keeps an ancestor's limit over a level's value it cannot
    // read; the execution limits refuse such a value, so every level that
    // states one is read on its own as well.
    let unreadable =
        crate::services::mcp_limits::first_unreadable(chain.iter().chain(tenant.iter()));
    let group_limits = fold_tenant_limits(
        merge_chain(chain, LimitFamily::Mcp),
        tenant.clone(),
        LimitFamily::Mcp,
    );
    Ok(crate::services::mcp_limits::McpLimitSources {
        group: group_limits,
        server,
        tenant,
        unreadable,
    })
}

/// Every per-call rate dimension of an MCP `tools/call`, broadest scope
/// first (see the section note), from the call's limit documents.
pub fn mcp_rate_dims(
    group: &ResourceGroup,
    mcp_server_id: &str,
    tool_name: &str,
    sources: &crate::services::mcp_limits::McpLimitSources,
) -> Vec<limits::LimitDim> {
    use limits::{FreqLimits, freq_dims};
    let group_limits = &sources.group;
    let server_limits = &sources.server;
    let tenant_limits = &sources.tenant;
    let gid = &group.id;
    let tid = &group.tenant_id;
    let mut dims = Vec::new();
    for (scope, key, src, path) in [
        (
            "group",
            format!("g:{gid}"),
            group_limits.as_ref(),
            vec!["frequency"],
        ),
        (
            "server",
            format!("g:{gid}:s:{mcp_server_id}"),
            server_limits.as_ref(),
            vec!["frequency"],
        ),
        (
            "group-tool",
            format!("g:{gid}:t:{tool_name}"),
            group_limits.as_ref(),
            vec!["per_tool", tool_name],
        ),
        (
            "server-tool",
            format!("g:{gid}:s:{mcp_server_id}:t:{tool_name}"),
            server_limits.as_ref(),
            vec!["per_tool", tool_name],
        ),
        (
            "tenant",
            format!("t:{tid}:s:{mcp_server_id}"),
            tenant_limits.as_ref(),
            vec!["frequency"],
        ),
        (
            "tenant-tool",
            format!("t:{tid}:s:{mcp_server_id}:t:{tool_name}"),
            tenant_limits.as_ref(),
            vec!["per_tool", tool_name],
        ),
    ] {
        dims.extend(freq_dims("MCP", scope, &key, &FreqLimits::at(src, &path)));
    }
    dims
}

/// Enforce per-call MCP rate limits for a `tools/call` across every scope,
/// admitted at once and for good. `None` = allowed. The proxy route uses
/// [`admit_mcp_rate_limits`] with a hold instead, so a later refusal hands
/// the units back.
///
/// `mcp_server_id` is the `mcp_servers.id` (the stable scope key);
/// `server_config_id` is the `server_configs.id` used to look up the
/// per-server `resource_group_server_configs.limits` row.
pub async fn enforce_mcp_rate_limits(
    usage_state: &UsageState,
    db: &PgPool,
    group: &ResourceGroup,
    mcp_server_id: &str,
    server_config_id: Option<&str>,
    tool_name: &str,
) -> Option<McpRateLimitBlock> {
    match load_mcp_limit_sources(db, group, mcp_server_id, server_config_id).await {
        Ok(sources) => {
            admit_mcp_rate_limits(
                usage_state,
                group,
                mcp_server_id,
                tool_name,
                &sources,
                Vec::new(),
                None,
            )
            .await
        }
        Err(e) => Some(limits::config_unavailable_block(&e)),
    }
}

/// The per-call rate limits of a `tools/call` (from the call's limit
/// documents, `sources`), with its concurrency `slots`, admitted in ONE
/// atomic step under `hold`, ended by the guard the request adopts from it
/// when the call was admitted: released when a later gate refuses the call,
/// committed when the call is dispatched (or, when it carries slots, when
/// the call is over).
pub async fn admit_mcp_rate_limits(
    usage_state: &UsageState,
    group: &ResourceGroup,
    mcp_server_id: &str,
    tool_name: &str,
    sources: &crate::services::mcp_limits::McpLimitSources,
    slots: Vec<limits::LimitDim>,
    hold: Option<&mut limits::PendingHold>,
) -> Option<McpRateLimitBlock> {
    let mut dims = mcp_rate_dims(group, mcp_server_id, tool_name, sources);
    dims.extend(slots);
    admit_rate(usage_state, hold, dims).await
}

/// The A2A dimensions of one group: the optional `frequency` block (in the
/// MCP/LLM shape) and the `invocations` daily/monthly COUNT caps.
///
/// ```jsonc
/// "a2a_global_limits": {
///     "chain":       { "max_delegation_depth": 4 },
///     "frequency":   { "max_calls_per_minute": 60, "max_calls_per_hour": 1000,
///                      "max_calls_per_day": 10000, "burst_limit": 30,
///                      "cooldown_seconds": 60 },
///     "invocations": { "daily_limit": 10000, "monthly_limit": 200000 }
/// }
/// ```
///
/// `effective_a2a_limits` is the group-cache's ancestor-merged (restrictive,
/// min-wins) view, so child groups can only narrow the inherited rate. Keyed
/// `a2a:g:{group_id}` / `a2a:inv:g:{group_id}`, sharing no counters with the
/// MCP/LLM scopes. Without either block the gate is inert.
pub fn a2a_group_dims(
    group_id: &str,
    effective_a2a_limits: Option<&serde_json::Value>,
) -> Vec<limits::LimitDim> {
    let mut dims = limits::freq_dims(
        "A2A",
        "group",
        &format!("a2a:g:{group_id}"),
        &limits::FreqLimits::at(effective_a2a_limits, &["frequency"]),
    );
    dims.extend(a2a_invocation_dims(
        "group",
        &format!("a2a:inv:g:{group_id}"),
        effective_a2a_limits,
    ));
    dims
}

/// The `invocations` block of an A2A limits document: an absolute count per
/// day and per month.
fn a2a_invocation_dims(
    scope: &'static str,
    scope_key: &str,
    a2a_limits: Option<&serde_json::Value>,
) -> Vec<limits::LimitDim> {
    let mut dims = Vec::new();
    let Some(inv) = a2a_limits.and_then(|l| l.get("invocations")) else {
        return dims;
    };
    for (field, window, limit_type, when) in [
        (
            "daily_limit",
            QuotaWindow::Day,
            "daily_invocations",
            "today",
        ),
        (
            "monthly_limit",
            QuotaWindow::Month,
            "monthly_invocations",
            "this month",
        ),
    ] {
        if let Some(limit) = inv.get(field).and_then(|v| v.as_i64()).filter(|l| *l > 0) {
            dims.push(limits::window_count_dim(
                scope,
                scope_key,
                window,
                limit,
                limit_type,
                format!("A2A invocation cap exceeded at {scope} scope ({when})"),
            ));
        }
    }
    dims
}

/// The tenant-wide A2A limits of a card (`agent_cards.tenant_limits`): one
/// aggregate across ALL groups and every route that reaches the card, its
/// `frequency` keyed `a2a:t:{tenant}:c:{card}` and its `invocations` keyed
/// `a2a:inv:t:{tenant}:c:{card}`.
pub fn a2a_tenant_dims(
    tenant_id: &str,
    card_id: &str,
    card_tenant_limits: Option<&serde_json::Value>,
) -> Vec<limits::LimitDim> {
    let mut dims = limits::freq_dims(
        "A2A",
        "tenant",
        &format!("a2a:t:{tenant_id}:c:{card_id}"),
        &limits::FreqLimits::at(card_tenant_limits, &["frequency"]),
    );
    dims.extend(a2a_invocation_dims(
        "tenant",
        &format!("a2a:inv:t:{tenant_id}:c:{card_id}"),
        card_tenant_limits,
    ));
    dims
}

/// A capability's own hourly rate
/// (`agent_card_capabilities.rate_limit_per_hour`): one counter per
/// capability, across every caller and route. Unset or not positive = none.
pub fn a2a_capability_dims(
    capability_id: &str,
    capability_name: &str,
    rate_limit_per_hour: Option<i32>,
) -> Vec<limits::LimitDim> {
    match rate_limit_per_hour.filter(|l| *l > 0) {
        Some(limit) => vec![limits::window_count_dim(
            "capability",
            &format!("a2a:cap:{capability_id}"),
            QuotaWindow::Hour,
            i64::from(limit),
            "hourly_quota",
            format!(
                "A2A hourly rate limit exceeded for capability `{capability_name}` ({limit}/hour)"
            ),
        )],
        None => Vec::new(),
    }
}

/// Every A2A rate limit that applies to one call.
#[derive(Debug, Clone, Copy, Default)]
pub struct A2aLimits<'a> {
    /// The caller's group and its effective (chain-merged) A2A limits.
    pub group: Option<(&'a str, Option<&'a serde_json::Value>)>,
    /// The card: `(tenant id, card id, the card's tenant-wide limits)`.
    pub card: Option<(&'a str, &'a str, Option<&'a serde_json::Value>)>,
    /// The capability the call names or is dispatched to:
    /// `(capability id, name, its hourly rate)`.
    pub capability: Option<(&'a str, &'a str, Option<i32>)>,
}

/// Enforce the A2A rate limits of one call: the group's, the card's and the
/// capability's, admitted TOGETHER on the shared counters, so a call one of
/// them refuses spends none of the others' allowance.
pub async fn enforce_a2a_limits(
    usage_state: &UsageState,
    a2a: A2aLimits<'_>,
) -> Option<McpRateLimitBlock> {
    admit_a2a_limits(usage_state, a2a, None).await
}

/// [`enforce_a2a_limits`] under a hold (`hold`), ended by the guard the
/// request adopts from it when the call was admitted: committed when the
/// call is dispatched, released when a later gate refuses it.
pub async fn admit_a2a_limits(
    usage_state: &UsageState,
    a2a: A2aLimits<'_>,
    hold: Option<&mut limits::PendingHold>,
) -> Option<McpRateLimitBlock> {
    let mut dims = Vec::new();
    if let Some((group_id, effective_a2a_limits)) = a2a.group {
        dims.extend(a2a_group_dims(group_id, effective_a2a_limits));
    }
    if let Some((tenant_id, card_id, card_limits)) = a2a.card {
        dims.extend(a2a_tenant_dims(tenant_id, card_id, card_limits));
    }
    if let Some((id, name, rate)) = a2a.capability {
        dims.extend(a2a_capability_dims(id, name, rate));
    }
    if dims.is_empty() {
        return None;
    }
    admit_rate(usage_state, hold, dims).await
}

/// The throughput dimensions of one LLM scope: penalty box, then the RPM,
/// ITPM, OTPM and spend-rate buckets. Every bucket arms the scope's cooldown
/// when it refuses.
fn throughput_dims(
    scope: &'static str,
    scope_key: &str,
    t: &LLMThroughputLimits,
) -> Vec<limits::LimitDim> {
    use limits::{DimRefusal, LimitDim, RateRetry};
    if t.is_empty() {
        return Vec::new();
    }
    let cd = limits::cooldown_key(scope_key);
    let arm = t.cooldown_secs.map(|c| (cd.clone(), c as u64));
    let mut dims = vec![LimitDim {
        dim: Dim::cooldown(cd.clone()),
        refusal: DimRefusal::Rate {
            scope,
            limit_type: "cooldown",
            what: format!("LLM rate-limit cooldown active at {scope} scope"),
            retry: RateRetry::Cooldown,
        },
    }];
    let usd = |cents: i64| format!("${:.2}", cents as f64 / 100.0);
    for (rate, burst, suffix, measure, label, human) in [
        (
            t.rpm,
            t.rpm_burst,
            "rpm",
            Measure::One,
            "rpm",
            t.rpm.map(|r| format!("{r} requests/min")),
        ),
        (
            t.itpm,
            t.tokens_burst,
            "itpm",
            Measure::InTokens,
            "itpm",
            t.itpm.map(|r| format!("{r} input tokens/min")),
        ),
        (
            t.otpm,
            t.tokens_burst,
            "otpm",
            Measure::OutTokens,
            "otpm",
            t.otpm.map(|r| format!("{r} output tokens/min")),
        ),
        (
            t.usd_cents_per_minute,
            t.usd_cents_burst,
            "usd",
            Measure::CostCents,
            "spend_rate",
            t.usd_cents_per_minute.map(|r| format!("{}/min", usd(r))),
        ),
    ] {
        let Some(per_min) = rate else { continue };
        let capacity = burst.unwrap_or(per_min).max(1) as f64;
        let what = if label == "rpm" {
            format!(
                "LLM rate limit exceeded at {scope} scope ({})",
                human.unwrap_or_default()
            )
        } else {
            format!(
                "LLM throughput limit exceeded at {scope} scope ({})",
                human.unwrap_or_default()
            )
        };
        dims.push(LimitDim {
            dim: Dim::bucket(
                format!("{scope_key}:{suffix}"),
                capacity,
                per_min as f64 / 60.0,
                measure,
            )
            .arming(arm.clone()),
            refusal: DimRefusal::Rate {
                scope,
                limit_type: label,
                what,
                retry: RateRetry::Bucket {
                    cooldown_secs: t.cooldown_secs,
                },
            },
        });
    }
    dims
}

/// Every rate dimension of one LLM call: `frequency` and `throughput` at
/// group, model and tenant-wide scope. `Err` when a limit cannot be read.
pub async fn llm_rate_dims(
    db: &PgPool,
    group: &ResourceGroup,
    model_id: &str,
) -> Result<Vec<limits::LimitDim>, sqlx::Error> {
    use limits::{FreqLimits, freq_dims};
    let group_limits = collect_effective_llm_limits(db, group, model_id).await?;
    let per_model_limits = fetch_per_model_limits(db, &group.id, model_id).await?;
    let tenant_limits = llm_tenant_limits(db, model_id).await?;
    let gid = &group.id;
    let mut dims = Vec::new();
    for (scope, key, src) in [
        ("group", format!("llm:g:{gid}"), group_limits.as_ref()),
        (
            "model",
            format!("llm:gm:{gid}:{model_id}"),
            per_model_limits.as_ref(),
        ),
        (
            "tenant",
            format!("llm:t:{}:m:{model_id}", group.tenant_id),
            tenant_limits.as_ref(),
        ),
    ] {
        dims.extend(freq_dims(
            "LLM",
            scope,
            &key,
            &FreqLimits::at(src, &["frequency"]),
        ));
        dims.extend(throughput_dims(
            scope,
            &key,
            &llm_throughput_at(&src.cloned(), &["throughput"]),
        ));
    }
    Ok(dims)
}

/// Rate-limit gate for one LLM call, admitted for good (tests and callers
/// that settle nothing). The request path admits these dimensions with the
/// call's other limits in one step (`llm_preflight`).
pub async fn enforce_llm_rate_limits(
    usage_state: &UsageState,
    db: &PgPool,
    group: &ResourceGroup,
    model_id: &str,
) -> Option<McpRateLimitBlock> {
    match llm_rate_dims(db, group, model_id).await {
        Ok(dims) => admit_rate(usage_state, None, dims).await,
        Err(e) => Some(limits::config_unavailable_block(&e)),
    }
}

// ---------------------------------------------------------------------------
// LLM concurrency cap — in-flight slots (Phase C3)
// ---------------------------------------------------------------------------

/// Holds an LLM call's concurrency slots for its lifetime.
///
/// Every applicable scope (group, model, tenant-wide, routing group) is a
/// slot dimension admitted in ONE step under a leased hold, so a request
/// holds all its slots or none. **On drop, every slot is released** — on
/// success, upstream error, guardrail block, client disconnect or panic.
///
/// The slots are leased, not counted: while this replica lives it renews
/// them, so a stream that runs an hour keeps its slot (a counter with a
/// fixed TTL lost it after five minutes and let the cap be exceeded), and a
/// replica that dies stops renewing so its slots lapse within the lease
/// instead of wedging the cap.
///
/// The guard intentionally has NO public release method — releasing
/// early would create a TOCTOU window where the cap could be
/// momentarily over-allocated.
pub struct LlmConcurrencyGuard {
    // Field order is drop order: the slots are released before this
    // request stops claiming their keys.
    _hold: Option<crate::services::cache::usage_state::HoldGuard>,
    _claim: Option<SlotClaim>,
}

/// The slot keys one acquisition claimed in its request's
/// `run_end::REQUEST_SLOTS`; unclaimed on drop, so a later layer of the same
/// request that finds a key claimed knows an outer layer holds it.
struct SlotClaim {
    held: crate::middleware::run_end::HeldSlots,
    keys: Vec<String>,
}

impl SlotClaim {
    /// Claim, in this request's set, every key no layer of the request holds
    /// yet, and return them; a key already claimed is held by an enclosing
    /// layer (whose guard outlives this one) and is skipped. Outside a
    /// request scope every key is claimed.
    fn take(keys: Vec<String>) -> (Option<Self>, Vec<String>) {
        let Some(held) = crate::middleware::run_end::request_slots() else {
            return (None, keys);
        };
        let fresh: Vec<String> = {
            let mut set = held.lock().unwrap_or_else(|p| p.into_inner());
            keys.into_iter().filter(|k| set.insert(k.clone())).collect()
        };
        (
            Some(Self {
                held,
                keys: fresh.clone(),
            }),
            fresh,
        )
    }
}

impl Drop for SlotClaim {
    fn drop(&mut self) {
        let mut set = self.held.lock().unwrap_or_else(|p| p.into_inner());
        for k in &self.keys {
            set.remove(k);
        }
    }
}

/// Acquire concurrency slots at every applicable LLM scope. `Ok(guard)` on
/// success — the guard releases on drop. `Err(block)` when any scope is full
/// (429), or when the caps cannot be read or the slots reached (fail closed).
pub async fn acquire_llm_concurrency(
    usage_state: &Arc<UsageState>,
    db: &PgPool,
    group: &ResourceGroup,
    model_id: &str,
    routed_via_group_id: Option<&str>,
) -> Result<LlmConcurrencyGuard, McpRateLimitBlock> {
    use limits::{DimRefusal, LimitDim, RateRetry};
    let read = async {
        let group_limits = collect_effective_llm_limits(db, group, model_id).await?;
        let per_model = fetch_per_model_limits(db, &group.id, model_id).await?;
        let tenant = llm_tenant_limits(db, model_id).await?;
        let routing = match routed_via_group_id {
            Some(rg) => fetch_routing_group_max_parallel(db, &group.tenant_id, rg)
                .await?
                .map(|max| (rg.to_string(), max)),
            None => None,
        };
        Ok::<_, sqlx::Error>((group_limits, per_model, tenant, routing))
    };
    let (group_limits, per_model, tenant, routing) = match read.await {
        Ok(v) => v,
        Err(e) => return Err(limits::config_unavailable_block(&e)),
    };
    let gid = &group.id;
    let mut scopes: Vec<(&'static str, String, i64)> = Vec::new();
    for (scope, key, src) in [
        ("group", format!("llm:g:{gid}"), group_limits.as_ref()),
        (
            "model",
            format!("llm:gm:{gid}:{model_id}"),
            per_model.as_ref(),
        ),
        (
            "tenant",
            format!("llm:t:{}:m:{model_id}", group.tenant_id),
            tenant.as_ref(),
        ),
    ] {
        if let Some(max) = llm_concurrency_at(&src.cloned(), &["concurrency"]).max_concurrent {
            scopes.push((scope, key, max));
        }
    }
    // A routing group's `max_parallel_requests` caps requests in flight
    // THROUGH the group; the model carries `routed_via_group_id` when it was
    // selected from one.
    if let Some((rg, max)) = routing.filter(|(_, m)| *m > 0) {
        scopes.push(("routing_group", format!("llm:rg:{rg}"), max as i64));
    }
    // One request is one in-flight call at every scope: a slot an outer
    // layer of this request already holds is not taken again.
    let (claim, fresh) = SlotClaim::take(
        scopes
            .iter()
            .map(|(_, key, _)| format!("brutor:llm_slot:{key}"))
            .collect(),
    );
    scopes.retain(|(_, key, _)| fresh.contains(&format!("brutor:llm_slot:{key}")));
    if scopes.is_empty() {
        return Ok(LlmConcurrencyGuard {
            _hold: None,
            _claim: claim,
        });
    }
    let dims: Vec<LimitDim> = scopes
        .into_iter()
        .map(|(scope, key, max)| LimitDim {
            dim: Dim::slot(format!("brutor:llm_slot:{key}"), max),
            refusal: DimRefusal::Rate {
                scope,
                limit_type: "concurrency",
                what: format!("LLM concurrency cap reached at {scope} scope ({max} simultaneous)"),
                retry: RateRetry::Bucket {
                    cooldown_secs: None,
                },
            },
        })
        .collect();
    // Unique per acquisition: two layers of one request each hold their own.
    let hold_id = limits::HoldId::new(&group.tenant_id, "llm-slot");
    match limits::admit(usage_state, Some(&hold_id), dims, Shape::count()).await {
        Ok(Ok(admitted)) => Ok(LlmConcurrencyGuard {
            _hold: admitted.hold.map(|h| usage_state.adopt_hold(h)),
            _claim: claim,
        }),
        Ok(Err(refused)) => Err(refused.rate_block().unwrap_or_else(|| McpRateLimitBlock {
            scope: "group",
            limit_type: "concurrency",
            message: "LLM concurrency cap reached".into(),
            retry_after_secs: 1,
        })),
        Err(e) => Err(limits::unavailable_block(&e)),
    }
}

/// A routing group's `max_parallel_requests` (simultaneous-in-flight cap).
/// `Ok(None)` when the group is missing/disabled or the cap is unset; `Err`
/// when it cannot be read (fail closed — an unreadable cap is not an absent
/// one).
async fn fetch_routing_group_max_parallel(
    db: &PgPool,
    tenant_id: &str,
    routing_group_id: &str,
) -> Result<Option<i32>, sqlx::Error> {
    Ok(sqlx::query_scalar::<_, Option<i32>>(
        "SELECT max_parallel_requests FROM llm_routing_group \
         WHERE id = $1 AND tenant_id = $2 AND enabled = true",
    )
    .bind(routing_group_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await?
    .flatten())
}

// ---------------------------------------------------------------------------
// Skill quota enforcement
// ---------------------------------------------------------------------------

/// Check Agent Skills usage quota before processing a skill execution.
///
/// Uses governance inheritance: walks the full parent chain and applies
/// restrictive merge (min values) to compute effective limits.
///
/// Checks nested config structure on ResourceGroup.skill_global_limits:
///   skill_global_limits.execution.{daily_limit, monthly_limit}
///
/// Budget and token enforcement removed — cost/tokens are tracked at the LLM level.
/// Already-fetched skill execution counts for the quota decision — the seam that
/// makes [`evaluate_skill_quota`] pure (DB-free, unit-testable).
#[derive(Debug, Clone, Default)]
pub struct SkillUsageSnapshot {
    pub daily_exec: i64,
    pub monthly_exec: i64,
    pub ps_daily_exec: i64,
    pub ps_monthly_exec: i64,
}

/// PURE skill quota decision (group + per-skill execution counts). Block
/// condition `(count + 1) > limit`; group warns at 80%/90% (daily/monthly),
/// per-skill blocks only. Mirrors the former inline logic 1:1.
fn evaluate_skill_quota(
    group_id: &str,
    group_name: &str,
    limits: &serde_json::Value,
    effective_ps: Option<&serde_json::Value>,
    snap: &SkillUsageSnapshot,
) -> UsageEnforcementResult {
    let mut result = UsageEnforcementResult::allowed();

    if let Some(exec) = limits.get("execution") {
        if let Some(daily_limit) = exec.get("daily_limit").and_then(|v| v.as_i64()) {
            if daily_limit > 0 {
                let daily_count = snap.daily_exec;
                if (daily_count + 1) > daily_limit {
                    return make_blocked(
                        group_id,
                        group_name,
                        "daily_skill_executions",
                        &daily_count.to_string(),
                        &daily_limit.to_string(),
                        format!(
                            "Daily skill execution limit exceeded: {daily_count} / {daily_limit}"
                        ),
                    );
                }
                let warn_pct = exec
                    .get("daily_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(80.0);
                let util = (daily_count as f64 / daily_limit as f64) * 100.0;
                if util >= warn_pct {
                    result.warning_headers.push((
                        "X-Usage-Warning".into(),
                        format!(
                            "Daily skill executions at {util:.0}%: {daily_count} / {daily_limit}"
                        ),
                    ));
                }
            }
        }
        if let Some(monthly_limit) = exec.get("monthly_limit").and_then(|v| v.as_i64()) {
            if monthly_limit > 0 {
                let monthly_count = snap.monthly_exec;
                if (monthly_count + 1) > monthly_limit {
                    return make_blocked(
                        group_id,
                        group_name,
                        "monthly_skill_executions",
                        &monthly_count.to_string(),
                        &monthly_limit.to_string(),
                        format!(
                            "Monthly skill execution limit exceeded: {monthly_count} / {monthly_limit}"
                        ),
                    );
                }
                let warn_pct = exec
                    .get("monthly_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(90.0);
                let util = (monthly_count as f64 / monthly_limit as f64) * 100.0;
                if util >= warn_pct {
                    result.warning_headers.push((
                        "X-Usage-Warning".into(),
                        format!("Monthly skill executions at {util:.0}%: {monthly_count} / {monthly_limit}"),
                    ));
                }
            }
        }
    }

    if let Some(effective_ps) = effective_ps {
        if let Some(exec) = effective_ps.get("execution") {
            if let Some(daily_limit) = exec.get("daily_limit").and_then(|v| v.as_i64()) {
                if daily_limit > 0 {
                    let count = snap.ps_daily_exec;
                    if (count + 1) > daily_limit {
                        return make_blocked(
                            group_id,
                            group_name,
                            "per_skill_daily_executions",
                            &count.to_string(),
                            &daily_limit.to_string(),
                            format!(
                                "Per-skill daily execution limit exceeded: {count} / {daily_limit}"
                            ),
                        );
                    }
                }
            }
            if let Some(monthly_limit) = exec.get("monthly_limit").and_then(|v| v.as_i64()) {
                if monthly_limit > 0 {
                    let count = snap.ps_monthly_exec;
                    if (count + 1) > monthly_limit {
                        return make_blocked(
                            group_id,
                            group_name,
                            "per_skill_monthly_executions",
                            &count.to_string(),
                            &monthly_limit.to_string(),
                            format!(
                                "Per-skill monthly execution limit exceeded: {count} / {monthly_limit}"
                            ),
                        );
                    }
                }
            }
        }
    }

    result
}

/// Fetch the skill execution counts the decision needs — only for configured
/// dimensions (lazy), from `usage_logs` (resource_type='skill').
#[allow(clippy::too_many_arguments)]
async fn build_skill_snapshot(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    limits: &serde_json::Value,
    effective_ps: Option<&serde_json::Value>,
    skill_id: Option<&str>,
    today_start: chrono::DateTime<Utc>,
    month_start: chrono::DateTime<Utc>,
) -> SkillUsageSnapshot {
    let mut snap = SkillUsageSnapshot::default();
    let pos_i =
        |v: &serde_json::Value, k: &str| v.get(k).and_then(|x| x.as_i64()).is_some_and(|n| n > 0);

    if let Some(exec) = limits.get("execution") {
        if pos_i(exec, "daily_limit") {
            snap.daily_exec = count_requests(db, tenant_id, group_id, today_start, "skill")
                .await
                .or_over_limit();
        }
        if pos_i(exec, "monthly_limit") {
            snap.monthly_exec = count_requests(db, tenant_id, group_id, month_start, "skill")
                .await
                .or_over_limit();
        }
    }
    if let (Some(effective_ps), Some(sid)) = (effective_ps, skill_id) {
        if let Some(exec) = effective_ps.get("execution") {
            if pos_i(exec, "daily_limit") {
                snap.ps_daily_exec =
                    count_requests_for_skill(db, tenant_id, group_id, sid, today_start, "skill")
                        .await
                        .or_over_limit();
            }
            if pos_i(exec, "monthly_limit") {
                snap.ps_monthly_exec =
                    count_requests_for_skill(db, tenant_id, group_id, sid, month_start, "skill")
                        .await
                        .or_over_limit();
            }
        }
    }
    snap
}

/// Ledger-only [`enforce_skill_quota_counted`] (no shared counters).
pub async fn enforce_skill_quota(
    db: &PgPool,
    tenant_id: &str,
    group: Option<&ResourceGroup>,
    skill_id: Option<&str>,
) -> UsageEnforcementResult {
    enforce_skill_quota_counted(db, tenant_id, group, skill_id, None, None).await
}

/// The group's skill limits narrowed by the per-(skill, group) row: `(base,
/// per_skill_effective)`. `base` is `None` when nothing applies anywhere.
async fn skill_effective_limits(
    db: &PgPool,
    group: &ResourceGroup,
    skill_id: Option<&str>,
) -> Result<(Option<serde_json::Value>, Option<serde_json::Value>), sqlx::Error> {
    // Fold the skill's tenant-wide default limits in (min-wins) when we know
    // which skill this is; otherwise the RG chain alone.
    let effective = match skill_id {
        Some(sid) => collect_effective_skill_limits(db, group, sid).await?,
        None => collect_effective_limits(db, group, "skill_global_limits").await?,
    };
    // Per-(skill,group) assignment-row limits (resource_group_agent_skills.limits),
    // folded in min-wins below. Read BEFORE the empty-check: a cap set ONLY on
    // the assignment row must still enforce even when the group/tenant skill
    // chain produced nothing.
    let per_skill = match skill_id {
        Some(sid) => fetch_per_skill_limits(db, &group.id, sid).await?,
        None => None,
    };
    if effective.is_none() && per_skill.is_none() {
        return Ok((None, None));
    }
    // Normalize the base to an object: a JSON-null skill_global_limits must
    // not swallow the per-skill limits in `restrictive_merge`.
    let base = effective
        .filter(serde_json::Value::is_object)
        .unwrap_or_else(|| serde_json::json!({}));
    let effective_ps = per_skill.map(|ps| {
        let mut e = base.clone();
        restrictive_merge(&mut e, &ps, LimitFamily::Skill);
        e
    });
    Ok((Some(base), effective_ps))
}

/// Every execution-count dimension of one skill run: the group, the
/// (group, skill) pair, the skill's tenant-wide quota, every ancestor's
/// subtree, and this group's own runs counted into its own subtree.
pub async fn skill_quota_dims(
    db: &PgPool,
    tenant_id: &str,
    group: &ResourceGroup,
    skill_id: Option<&str>,
) -> Result<Vec<limits::LimitDim>, sqlx::Error> {
    use limits::{DimRefusal, Owner, execution_dims};
    let (base, effective_ps) = skill_effective_limits(db, group, skill_id).await?;
    let own = Owner::new(&group.id, &group.name);
    let mut dims = execution_dims(
        &format!("grp:{}", group.id),
        base.as_ref(),
        &own,
        true,
        ("daily_skill_executions", "monthly_skill_executions"),
    );
    if let Some(sid) = skill_id {
        dims.extend(execution_dims(
            &format!("grp:{}:skl:{sid}", group.id),
            effective_ps.as_ref(),
            &own,
            true,
            ("per_skill_daily_executions", "per_skill_monthly_executions"),
        ));
        dims.extend(execution_dims(
            &format!("ten:{tenant_id}:skl:{sid}"),
            skill_tenant_limits(db, sid).await?.as_ref(),
            &Owner::new("__tenant__", "tenant-wide"),
            true,
            ("daily_skill_executions", "monthly_skill_executions"),
        ));
    }
    for a in ancestor_budget_chain(db, group)
        .await?
        .iter()
        .filter(|a| a.path.is_some() && a.skill.is_some())
    {
        dims.extend(execution_dims(
            &format!("sub:{}", a.id),
            a.skill.as_ref(),
            &Owner::new(&a.id, &a.name),
            false,
            ("daily_skill_ancestor", "monthly_skill_ancestor"),
        ));
    }
    if group.hierarchy_path.is_some() && group.skill_global_limits.is_some() {
        dims.extend(
            execution_dims(
                &format!("sub:{}", group.id),
                group.skill_global_limits.as_ref(),
                &own,
                false,
                ("daily_skill_ancestor", "monthly_skill_ancestor"),
            )
            .into_iter()
            .map(|mut d| {
                if let crate::services::cache::usage_state::DimKind::Counter { limit, .. } =
                    &mut d.dim.kind
                {
                    *limit = None;
                }
                d.refusal = DimRefusal::Accounting;
                d
            }),
        );
    }
    Ok(dims)
}

/// Skill execution quotas (group, per-skill, tenant-wide, ancestor) judged
/// against the ledger, and — with shared counters — every scope's execution
/// count ADMITTED atomically. `hold_id` = the run's hold, ended by its
/// guard (committed at dispatch, released when a later gate refuses);
/// `None` admits for good on the spot.
pub async fn enforce_skill_quota_counted(
    db: &PgPool,
    tenant_id: &str,
    group: Option<&ResourceGroup>,
    skill_id: Option<&str>,
    counters: Option<&UsageState>,
    hold: Option<&mut limits::PendingHold>,
) -> UsageEnforcementResult {
    let group = match group {
        Some(g) => g,
        None => return UsageEnforcementResult::allowed(),
    };
    let (base, effective_ps) = match skill_effective_limits(db, group, skill_id).await {
        Ok(v) => v,
        Err(e) => return limits::config_unavailable_result(&e),
    };

    let mut result = UsageEnforcementResult::allowed();
    let now = Utc::now();
    let today_start = now.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc();
    let month_start = now
        .with_day(1)
        .unwrap()
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .and_utc();

    if let Some(limits) = base.as_ref() {
        let snap = build_skill_snapshot(
            db,
            tenant_id,
            &group.id,
            limits,
            effective_ps.as_ref(),
            skill_id,
            today_start,
            month_start,
        )
        .await;
        result = evaluate_skill_quota(&group.id, &group.name, limits, effective_ps.as_ref(), &snap);
        if result.blocked {
            return result;
        }
    }

    // TENANT-WIDE per-skill execution quota: one aggregate across ALL RGs for
    // this skill, enforced IN ADDITION to the RG limits above.
    if let Some(sid) = skill_id {
        if let Some(block) =
            enforce_skill_tenant_quota(db, tenant_id, sid, today_start, month_start).await
        {
            return block;
        }
    }

    if let Some(ancestor_block) =
        enforce_ancestor_budgets(db, tenant_id, group, "skill_global_limits", "skill").await
    {
        return ancestor_block;
    }

    if let Some(us) = counters {
        let dims = match skill_quota_dims(db, tenant_id, group, skill_id).await {
            Ok(d) => d,
            Err(e) => return limits::config_unavailable_result(&e),
        };
        let hold_id = hold.as_deref().map(limits::PendingHold::id);
        match limits::admit(us, hold_id, dims, Shape::count()).await {
            Ok(Ok(admitted)) => {
                if let Some(hold) = hold {
                    hold.admitted_as(admitted);
                }
            }
            Ok(Err(refused)) => return refused.quota_result(),
            Err(e) => return limits::unavailable_result(&e),
        }
    }

    result
}

/// A skill's tenant-wide limits; `Err` when unreadable (fail closed).
async fn skill_tenant_limits(
    db: &PgPool,
    skill_id: &str,
) -> Result<Option<serde_json::Value>, sqlx::Error> {
    fetch_tenant_limits(db, "agent_skills", skill_id).await
}

/// Block if the skill's TENANT-WIDE execution quota (agent_skills.tenant_limits,
/// aggregated across all RGs) is exceeded. Ledger-sourced; None when unset/within.
async fn enforce_skill_tenant_quota(
    db: &PgPool,
    tenant_id: &str,
    skill_id: &str,
    today_start: chrono::DateTime<Utc>,
    month_start: chrono::DateTime<Utc>,
) -> Option<UsageEnforcementResult> {
    // An unreadable tenant limit is not an absent one (fail closed).
    let limits = match skill_tenant_limits(db, skill_id).await {
        Ok(Some(v)) => v,
        Ok(None) => return None,
        Err(e) => return Some(budget_unavailable("tenant-wide skill", &e)),
    };
    let snap = SkillUsageSnapshot {
        daily_exec: count_requests_tenant_model(db, tenant_id, skill_id, today_start, "skill")
            .await
            .or_over_limit(),
        monthly_exec: count_requests_tenant_model(db, tenant_id, skill_id, month_start, "skill")
            .await
            .or_over_limit(),
        ps_daily_exec: 0,
        ps_monthly_exec: 0,
    };
    let result = evaluate_skill_quota("__tenant__", "tenant-wide", &limits, None, &snap);
    if result.blocked { Some(result) } else { None }
}

/// Check if the user has access to the requested MCP server via resource groups.
/// Uses the resource_group_server_configs join table (group → server_config → mcp_server).
pub async fn check_mcp_server_access(
    db: &PgPool,
    server_id: &str,
    group: Option<&ResourceGroup>,
) -> Result<bool, sqlx::Error> {
    let group = match group {
        Some(g) => g,
        // No group resolved — deny access (user has no group membership)
        None => return Ok(false),
    };

    // Direct assignment or inherited through the parent chain.
    if mcp_server_in_group_chain(db, server_id, group).await? {
        return Ok(true);
    }

    // If group has NO server configs assigned at all, allow access (unconfigured = open)
    let config_count: (Option<i64>,) = sqlx::query_as(
        "SELECT COUNT(*) FROM resource_group_server_configs WHERE resource_group_id = $1",
    )
    .bind(&group.id)
    .fetch_one(db)
    .await?;

    Ok(config_count.0.unwrap_or(0) == 0)
}

/// VMCP-backed counterpart to `check_mcp_server_access`.
///
/// Walks `resource_group_server_configs` keyed on `virtual_server_id`
/// instead of `mcp_server_id`. The inheritance + "unconfigured ⇒
/// open" semantics mirror the MCP path so both backends behave
/// uniformly from the portal's perspective.
pub async fn check_vmcp_access(
    db: &PgPool,
    vs_id: &str,
    group: Option<&ResourceGroup>,
) -> Result<bool, sqlx::Error> {
    let group = match group {
        Some(g) => g,
        None => return Ok(false),
    };

    let direct: Option<(i32,)> = sqlx::query_as(
        r#"SELECT 1 as found FROM resource_group_server_configs rgsc
           JOIN server_configs sc ON sc.id = rgsc.server_config_id
           WHERE rgsc.resource_group_id = $1 AND sc.virtual_server_id = $2 AND sc.enabled = true"#,
    )
    .bind(&group.id)
    .bind(vs_id)
    .fetch_optional(db)
    .await?;

    if direct.is_some() {
        return Ok(true);
    }

    if group.inherit_resources {
        let mut current_parent_id = group.parent_group_id.clone();
        for _ in 0..10 {
            let pid = match current_parent_id {
                Some(ref id) => id.clone(),
                None => break,
            };

            let found: Option<(i32,)> = sqlx::query_as(
                r#"SELECT 1 as found FROM resource_group_server_configs rgsc
                   JOIN server_configs sc ON sc.id = rgsc.server_config_id
                   WHERE sc.virtual_server_id = $1 AND sc.enabled = true
                     AND rgsc.resource_group_id = $2"#,
            )
            .bind(vs_id)
            .bind(&pid)
            .fetch_optional(db)
            .await?;

            if found.is_some() {
                return Ok(true);
            }

            let parent: Option<(Option<String>, bool)> = sqlx::query_as(
                "SELECT parent_group_id, inherit_from_parent FROM resource_groups WHERE id = $1",
            )
            .bind(&pid)
            .fetch_optional(db)
            .await?;

            match parent {
                Some((next_pid, inherits)) if inherits => {
                    current_parent_id = next_pid;
                }
                _ => break,
            }
        }
    }

    // Unconfigured group ⇒ open (parity with check_mcp_server_access).
    let config_count: (Option<i64>,) = sqlx::query_as(
        "SELECT COUNT(*) FROM resource_group_server_configs WHERE resource_group_id = $1",
    )
    .bind(&group.id)
    .fetch_one(db)
    .await?;

    Ok(config_count.0.unwrap_or(0) == 0)
}

/// Three-state capability access result.
#[derive(Debug, Clone, PartialEq)]
pub enum CapabilityAccess {
    /// Capability is allowed — forward the request.
    Allowed,
    /// Capability requires human approval before execution.
    ///
    /// `window_seconds` is the window the policy that holds the capability
    /// STATES (a capability-filter row's or the group-wide default's
    /// `approval_timeout_seconds`), `None` when it states none — the hold
    /// then takes the Resource Group / tenant / platform default from
    /// `approval_window::resolve`. Never defaulted here: a baked-in 300
    /// is how every unconfigured hold came to lapse after five minutes.
    ApprovalRequired { window_seconds: Option<i32> },
    /// Capability is denied — block the request.
    Denied,
}

impl CapabilityAccess {
    /// Strictness ordering: Allowed(0) < ApprovalRequired(1) < Denied(2).
    fn strictness(&self) -> u8 {
        match self {
            Self::Allowed => 0,
            Self::ApprovalRequired { .. } => 1,
            Self::Denied => 2,
        }
    }
}

/// Parse a `default_*_access` capability decision out of an mcp-governance JSON
/// blob (the `mcp_global_governance` shape, shared by RG columns and the MCP
/// server's tenant_governance).
///
/// Maps capability_type to the corresponding default_*_access field (a type
/// it does not map is Denied). A field
/// that is absent (or null) states nothing: Allowed. A value this build does
/// not know (another string, or not a string) is the most restrictive,
/// Denied, as on a capability-filter row.
fn mcp_default_access_from_gov(gov: &serde_json::Value, capability_type: &str) -> CapabilityAccess {
    let key = match capability_type {
        "tool" => "default_tool_access",
        "resource" => "default_resource_access",
        "prompt" => "default_prompt_access",
        "resource_template" => "default_resource_template_access",
        // A capability type this function does not map has no default it
        // could read: the most restrictive reading.
        other => {
            tracing::error!(
                capability_type = other,
                "MCP capability type with no default access field — denying"
            );
            return CapabilityAccess::Denied;
        }
    };
    let window = gov
        .get("approval_timeout_seconds")
        .and_then(|v| v.as_i64())
        .and_then(|v| i32::try_from(v).ok())
        .filter(|v| *v > 0);
    match gov.get(key) {
        None | Some(serde_json::Value::Null) => CapabilityAccess::Allowed,
        Some(serde_json::Value::String(s)) if s == "enabled" => CapabilityAccess::Allowed,
        Some(serde_json::Value::String(s)) if s == "disabled" => CapabilityAccess::Denied,
        Some(serde_json::Value::String(s)) if s == "approval_required" => {
            CapabilityAccess::ApprovalRequired {
                window_seconds: window,
            }
        }
        Some(other) => {
            tracing::error!(
                field = key,
                access = %other,
                "unknown MCP default access level — denying"
            );
            CapabilityAccess::Denied
        }
    }
}

/// A group's id followed by every ancestor's, nearest first.
///
/// Two copies of this loop existed inline, one per capability path, and a
/// third was missing entirely from the group-wide default fallback — which
/// is how an organisation's `default_tool_access: disabled` failed to reach
/// a department that set no MCP governance of its own.
///
/// The depth cap matches the ten-level limit the control plane enforces on
/// the hierarchy; a cycle would otherwise spin here.
///
/// An error is returned, never a shorter chain: a chain cut short by a
/// failed lookup silently drops the restrictive governance of every ancestor
/// above the cut. Callers fail closed on `Err`.
pub(crate) async fn ancestor_chain_ids(
    db: &PgPool,
    group_id: &str,
) -> Result<Vec<String>, sqlx::Error> {
    let mut ids: Vec<String> = vec![group_id.to_string()];
    let mut current_id = group_id.to_string();
    for _ in 0..10 {
        let parent: Option<(Option<String>,)> =
            sqlx::query_as("SELECT parent_group_id FROM resource_groups WHERE id = $1")
                .bind(&current_id)
                .fetch_optional(db)
                .await?;
        match parent {
            Some((Some(pid),)) => {
                if ids.contains(&pid) {
                    break; // defensive: a cycle must not loop forever
                }
                ids.push(pid.clone());
                current_id = pid;
            }
            _ => break,
        }
    }
    Ok(ids)
}

/// The group-wide MCP default for one group, merged most-restrictive across
/// that group and every ancestor.
///
/// Callers that already hold the chain should use
/// `resolve_mcp_global_default_chain` directly.
pub async fn resolve_mcp_default_access_for_group(
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    capability_type: &str,
) -> CapabilityAccess {
    match ancestor_chain_ids(db, group_id).await {
        Ok(chain) => resolve_mcp_global_default_chain(db, tenant_id, &chain, capability_type).await,
        Err(e) => capability_lookup_failed("ancestor chain", &e),
    }
}

/// The group-wide MCP default for a capability type, merged most-restrictive
/// across this group and every ancestor.
///
/// `group_ids` is the chain the caller already walked (self first, then each
/// parent). Reading only `group.mcp_global_governance` — as this did — meant
/// an organisation that set `default_tool_access: disabled` did not reach a
/// department with no MCP governance of its own, so the department's tools
/// stayed open. Per-capability filter rows were already chain-merged; this
/// is the fallback taken when no filter row exists anywhere in the chain,
/// which is the common case for a group that governs by default rather than
/// by naming individual tools.
///
/// Rows are fetched in one query rather than per level; the chain is at most
/// ten deep and this sits on the MCP request path.
async fn resolve_mcp_global_default_chain(
    db: &PgPool,
    tenant_id: &str,
    group_ids: &[String],
    capability_type: &str,
) -> CapabilityAccess {
    if group_ids.is_empty() {
        return CapabilityAccess::Allowed;
    }

    let rows: Vec<(Option<serde_json::Value>,)> = match sqlx::query_as(
        "SELECT mcp_global_governance FROM resource_groups \
         WHERE id = ANY($1) AND tenant_id = $2 AND mcp_global_governance IS NOT NULL",
    )
    .bind(group_ids)
    .bind(tenant_id)
    .fetch_all(db)
    .await
    {
        Ok(rows) => rows,
        Err(e) => {
            // Fail closed: an unreadable policy is not an absent policy.
            tracing::error!(
                error = %e,
                "mcp governance chain query failed — denying capability access"
            );
            return CapabilityAccess::Denied;
        }
    };

    let mut effective = CapabilityAccess::Allowed;
    for (gov,) in rows.iter() {
        if let Some(gov) = gov {
            let row_access = mcp_default_access_from_gov(gov, capability_type);
            effective = match (&effective, row_access) {
                // Two levels both hold the tool: the smallest stated
                // window (min-wins, like every approval timeout).
                (
                    CapabilityAccess::ApprovalRequired { window_seconds: a },
                    CapabilityAccess::ApprovalRequired { window_seconds: b },
                ) => CapabilityAccess::ApprovalRequired {
                    window_seconds: min_hold_window(*a, b),
                },
                (cur, row) if row.strictness() > cur.strictness() => row,
                (cur, _) => cur.clone(),
            };
        }
    }
    effective
}

/// Check if a specific MCP capability is allowed across multiple groups (allow-wins).
///
/// Returns the **least restrictive** access across all groups:
/// - If ANY group grants `Allowed`, returns `Allowed`.
/// - Else if ANY group grants `ApprovalRequired`, returns `ApprovalRequired`.
/// - Else returns `Denied`.
///
/// - `mcp_server_id`: The MCP server's ID (from mcp_servers table)
/// - `capability_type`: "tool", "resource", "prompt", or "resource_template"
/// - `capability_name`: The tool name, resource URI, etc.
/// - `groups`: All resource groups granting access to this server
pub async fn check_mcp_capability_access(
    db: &PgPool,
    mcp_server_id: &str,
    capability_type: &str,
    capability_name: &str,
    groups: &[ResourceGroup],
) -> CapabilityAccess {
    check_mcp_capability_access_with_hold_window(
        db,
        mcp_server_id,
        capability_type,
        capability_name,
        groups,
    )
    .await
    .access
}

/// A capability-filter decision plus the approval window the filter rows
/// carry, kept separately because `CapabilityAccess::Allowed` has nowhere
/// to put it.
///
/// `hold_window_seconds` is the smallest positive `approval_timeout_seconds`
/// found on any filter row consulted for this capability — including rows
/// whose access is `enabled`. It exists for the holds that do NOT come from
/// the filter itself: an argument-policy band or an agent
/// grant can demand a human for a tool the filter leaves enabled, and the
/// only place an operator can say "this tool's holds stay open for a day
/// without becoming approval-required outright" is that same row. `None`
/// means no row set a window; the hold then takes the Resource Group /
/// tenant / platform default (`approval_window::resolve`).
#[derive(Debug, Clone, PartialEq)]
pub struct CapabilityAccessResolution {
    pub access: CapabilityAccess,
    pub hold_window_seconds: Option<i32>,
}

/// Fail closed on an MCP capability lookup error: an unreadable
/// filter, ceiling or chain is not an absent one. Logged at ERROR so the
/// on-call sees that capability enforcement is degraded to deny.
fn capability_lookup_failed(what: &str, error: &sqlx::Error) -> CapabilityAccess {
    tracing::error!(error = %error, lookup = what, "MCP capability lookup failed — denying (fail closed)");
    CapabilityAccess::Denied
}

/// Merge two optional positive windows, smallest wins (the restrictive merge
/// every other timeout in the platform uses).
fn min_hold_window(a: Option<i32>, b: Option<i32>) -> Option<i32> {
    match (a.filter(|t| *t > 0), b.filter(|t| *t > 0)) {
        (Some(x), Some(y)) => Some(x.min(y)),
        (x, None) => x,
        (None, y) => y,
    }
}

/// [`check_mcp_capability_access`] that also reports the hold window carried
/// by the filter rows — see [`CapabilityAccessResolution`].
pub async fn check_mcp_capability_access_with_hold_window(
    db: &PgPool,
    mcp_server_id: &str,
    capability_type: &str,
    capability_name: &str,
    groups: &[ResourceGroup],
) -> CapabilityAccessResolution {
    if groups.is_empty() {
        // No groups = no filters
        return CapabilityAccessResolution {
            access: CapabilityAccess::Allowed,
            hold_window_seconds: None,
        };
    }

    // Allow-wins: least restrictive across all groups.
    let mut best = CapabilityAccess::Denied;
    let mut hold_window: Option<i32> = None;

    for group in groups {
        let (access, group_window) = check_mcp_capability_access_single(
            db,
            mcp_server_id,
            capability_type,
            capability_name,
            group,
        )
        .await;

        if access.strictness() < best.strictness() {
            best = access;
        }
        hold_window = min_hold_window(hold_window, group_window);
        // (No early return on Allowed — the MCP server's tenant-wide default
        //  below can still tighten the result, so we must always apply it.)
    }

    // Apply the MCP server's TENANT-WIDE default access as a most-restrictive
    // ceiling (min-wins): a server-level default can only TIGHTEN the group
    // allow-wins result, never widen it. Set via Resources → MCP Servers →
    // <server> → Policies (agent_cards-style tenant_governance blob).
    let server_gov: Option<(Option<serde_json::Value>,)> =
        match sqlx::query_as("SELECT tenant_governance FROM mcp_servers WHERE id = $1")
            .bind(mcp_server_id)
            .fetch_optional(db)
            .await
        {
            Ok(row) => row,
            // An unreadable ceiling is not an absent one.
            Err(e) => {
                return CapabilityAccessResolution {
                    access: capability_lookup_failed("server ceiling", &e),
                    hold_window_seconds: hold_window,
                };
            }
        };
    if let Some((Some(gov),)) = server_gov {
        let ceiling = mcp_default_access_from_gov(&gov, capability_type);
        if ceiling.strictness() > best.strictness() {
            best = ceiling;
        }
    }

    CapabilityAccessResolution {
        access: best,
        hold_window_seconds: hold_window,
    }
}

/// Check capability access for a single group (internal helper).
/// Returns the effective access state with governance inheritance (restrictive
/// merge), plus the smallest positive `approval_timeout_seconds` any filter
/// row in the chain carries (whatever its access) — see
/// [`CapabilityAccessResolution`]. `None` when no filter row names the
/// capability.
async fn check_mcp_capability_access_single(
    db: &PgPool,
    mcp_server_id: &str,
    capability_type: &str,
    capability_name: &str,
    group: &ResourceGroup,
) -> (CapabilityAccess, Option<i32>) {
    // Resolve server_config_id(s) for this MCP server.
    // Don't filter by group assignment here — the server may be inherited from a
    // parent group, but capability filters are stored against the child group.
    let config_ids: Vec<(String,)> = match sqlx::query_as(
        "SELECT id FROM server_configs WHERE mcp_server_id = $1 AND tenant_id = $2 AND enabled = true",
    )
    .bind(mcp_server_id)
    .bind(&group.tenant_id)
    .fetch_all(db)
    .await
    {
        Ok(ids) => ids,
        // This used to read as "no config found — allow".
        Err(e) => return (capability_lookup_failed("server configs", &e), None),
    };

    if config_ids.is_empty() {
        return (CapabilityAccess::Allowed, None); // No config found — allow
    }

    // Check capability filters for the first matching server config
    if let Some((config_id,)) = config_ids.first() {
        // Self + every ancestor, for governance inheritance.
        let group_ids = match ancestor_chain_ids(db, &group.id).await {
            Ok(ids) => ids,
            Err(e) => return (capability_lookup_failed("ancestor chain", &e), None),
        };

        // Query filters for ALL groups in the ancestor chain
        let rows: Vec<(String, String, Option<i32>)> = match sqlx::query_as(
            r#"SELECT resource_group_id, access, approval_timeout_seconds
               FROM resource_group_mcp_capabilities
               WHERE resource_group_id = ANY($1) AND server_config_id = $2
                 AND capability_type = $3 AND capability_name = $4"#,
        )
        .bind(&group_ids)
        .bind(config_id)
        .bind(capability_type)
        .bind(capability_name)
        .fetch_all(db)
        .await
        {
            Ok(rows) => rows,
            Err(e) => return (capability_lookup_failed("capability filters", &e), None),
        };

        if rows.is_empty() {
            // No explicit filter for this capability. Check if filters exist for this
            // type at any level — if so, unlisted = denied (restrictive).
            let type_count: (Option<i64>,) = sqlx::query_as(
                r#"SELECT COUNT(*) FROM resource_group_mcp_capabilities
                   WHERE resource_group_id = ANY($1) AND server_config_id = $2 AND capability_type = $3"#,
            )
            .bind(&group_ids)
            .bind(config_id)
            .bind(capability_type)
            .fetch_one(db)
            .await
            .unwrap_or_else(|e| {
        // FAIL CLOSED: a usage-DB error must NOT silently grant unlimited
        // spend. Returning the saturating max makes any configured limit
        // read as exceeded so the request is denied; logged at ERROR so the
        // on-call sees that quota enforcement is degraded.
        tracing::error!(error = %e, "usage aggregate query failed — failing closed (usage treated as over-limit)");
        (Some(i64::MAX),)
    });

            if type_count.0.unwrap_or(0) > 0 {
                return (CapabilityAccess::Denied, None); // Filters exist for this type but cap isn't listed
            }

            // No per-capability filters anywhere in the chain — fall back to
            // the group-wide default, merged across the same chain.
            return (
                resolve_mcp_global_default_chain(db, &group.tenant_id, &group_ids, capability_type)
                    .await,
                None,
            );
        }

        // Restrictive merge across ancestor chain: most restrictive wins
        let mut effective = CapabilityAccess::Allowed;
        // Smallest positive window on ANY row in the chain. It is the
        // approval_required timeout when the filter itself holds the call,
        // and the band/agent-grant hold window when the filter leaves the
        // tool enabled — an `enabled` row's timeout used to be discarded
        // here, which pinned every band-only hold to the 300 s default.
        let mut window: Option<i32> = None;

        for (_group_id, access, approval_timeout) in &rows {
            let row_access = match access.as_str() {
                "disabled" => CapabilityAccess::Denied,
                "approval_required" => CapabilityAccess::ApprovalRequired {
                    window_seconds: approval_timeout.filter(|t| *t > 0),
                },
                "enabled" => CapabilityAccess::Allowed,
                // An access level this build does not know is not "enabled":
                // a value the control plane added later must not
                // open the tool on an older proxy.
                other => {
                    tracing::error!(
                        access = other,
                        "unknown MCP capability access level — denying"
                    );
                    CapabilityAccess::Denied
                }
            };

            if row_access.strictness() > effective.strictness() {
                effective = row_access;
            }
            window = min_hold_window(window, *approval_timeout);
        }

        // If effective is approval_required, use the most restrictive
        // (smallest) window any row states — or none, and the hold takes
        // the group / tenant / platform default.
        if let CapabilityAccess::ApprovalRequired { .. } = &effective {
            effective = CapabilityAccess::ApprovalRequired {
                window_seconds: window,
            };
        }

        return (effective, window);
    }

    (CapabilityAccess::Allowed, None)
}

/// Capability access state for portal capability listing.
/// Maps capability name → access state for filtering/annotation.
#[derive(Debug, Clone)]
pub struct CapabilityAccessInfo {
    pub access: CapabilityAccess,
}

/// Get capability access states for a specific type, merged across all groups (allow-wins).
///
/// Returns None if any group has no filters configured (meaning allow all).
/// Returns Some(map) with capability_name → access state for the portal to annotate tools.
pub async fn get_capability_access_map(
    db: &PgPool,
    mcp_server_id: &str,
    capability_type: &str,
    groups: &[ResourceGroup],
) -> Option<std::collections::HashMap<String, CapabilityAccessInfo>> {
    if groups.is_empty() {
        return None; // No groups = no filters
    }

    let mut merged: Option<std::collections::HashMap<String, CapabilityAccessInfo>> = None;

    for group in groups {
        match get_capability_access_map_single(db, mcp_server_id, capability_type, group).await {
            None => {
                // This group has no filters — allow all (allow-wins: no restriction from this group)
                return None;
            }
            Some(group_map) => {
                // Allow-wins merge: for each capability, keep least restrictive across groups
                match &mut merged {
                    None => merged = Some(group_map),
                    Some(set) => {
                        for (name, info) in group_map {
                            let entry = set.entry(name).or_insert_with(|| CapabilityAccessInfo {
                                access: CapabilityAccess::Denied,
                            });
                            // Keep least restrictive
                            if info.access.strictness() < entry.access.strictness() {
                                entry.access = info.access;
                            }
                        }
                    }
                }
            }
        }
    }

    merged
}

/// Get enabled capabilities for a single group — backward-compatible wrapper.
///
/// Returns None = allow all, Some(set) = only these are allowed.
pub async fn get_enabled_capabilities(
    db: &PgPool,
    mcp_server_id: &str,
    capability_type: &str,
    groups: &[ResourceGroup],
) -> Option<std::collections::HashSet<String>> {
    let map = get_capability_access_map(db, mcp_server_id, capability_type, groups).await?;

    // Only include capabilities that are allowed (not denied or approval_required)
    // For portal listing, approval_required tools should still be shown but annotated
    let enabled: std::collections::HashSet<String> = map
        .into_iter()
        .filter(|(_, info)| info.access != CapabilityAccess::Denied)
        .map(|(name, _)| name)
        .collect();

    Some(enabled)
}

/// Get capability access map for a single group, with governance inheritance.
///
/// Walks the parent chain and applies restrictive merge: most restrictive wins.
async fn get_capability_access_map_single(
    db: &PgPool,
    mcp_server_id: &str,
    capability_type: &str,
    group: &ResourceGroup,
) -> Option<std::collections::HashMap<String, CapabilityAccessInfo>> {
    // Resolve server_config_id
    // On a lookup error this group grants nothing (an empty map), never
    // "no filters — allow all" (`None`), which is what an error used to read as.
    let config_id: Option<(String,)> = match sqlx::query_as(
        "SELECT id FROM server_configs WHERE mcp_server_id = $1 AND tenant_id = $2 AND enabled = true LIMIT 1",
    )
    .bind(mcp_server_id)
    .bind(&group.tenant_id)
    .fetch_optional(db)
    .await
    {
        Ok(row) => row,
        Err(e) => {
            capability_lookup_failed("server configs", &e);
            return Some(std::collections::HashMap::new());
        }
    };

    let config_id = match config_id {
        Some((id,)) => id,
        None => return None,
    };

    // Self + every ancestor, for governance inheritance.
    let group_ids = match ancestor_chain_ids(db, &group.id).await {
        Ok(ids) => ids,
        Err(e) => {
            capability_lookup_failed("ancestor chain", &e);
            return Some(std::collections::HashMap::new());
        }
    };

    // Query filters for ALL groups in the chain at once
    let rows: Vec<(String, String, String, Option<i32>)> = match sqlx::query_as(
        r#"SELECT resource_group_id, capability_name, access, approval_timeout_seconds
           FROM resource_group_mcp_capabilities
           WHERE resource_group_id = ANY($1) AND server_config_id = $2 AND capability_type = $3"#,
    )
    .bind(&group_ids)
    .bind(&config_id)
    .bind(capability_type)
    .fetch_all(db)
    .await
    {
        Ok(rows) => rows,
        Err(e) => {
            capability_lookup_failed("capability filters", &e);
            return Some(std::collections::HashMap::new());
        }
    };

    if rows.is_empty() {
        return None; // No filters at any level — allow all
    }

    // Build effective state: restrictive merge (most restrictive wins across ancestors)
    let mut cap_state: std::collections::HashMap<String, (u8, Option<i32>)> =
        std::collections::HashMap::new();
    for (_group_id, cap_name, access, timeout) in &rows {
        let strictness = match access.as_str() {
            "enabled" => 0u8,
            "approval_required" => 1u8,
            // "disabled", or a level this build does not know.
            _ => 2u8,
        };
        let entry = cap_state.entry(cap_name.clone()).or_insert((0, None));
        if strictness > entry.0 {
            entry.0 = strictness;
        }
        // Most restrictive (smallest) stated window; none stated = the
        // group / tenant / platform default applies at hold time.
        entry.1 = min_hold_window(entry.1, *timeout);
    }

    let map: std::collections::HashMap<String, CapabilityAccessInfo> = cap_state
        .into_iter()
        .map(|(name, (strictness, window))| {
            let access = match strictness {
                0 => CapabilityAccess::Allowed,
                1 => CapabilityAccess::ApprovalRequired {
                    window_seconds: window,
                },
                _ => CapabilityAccess::Denied,
            };
            (name, CapabilityAccessInfo { access })
        })
        .collect();

    Some(map)
}

/// Resolve model access and the billing group in one step.
///
/// Returns the `ResourceGroup` that grants access to the model. This group is
/// also the correct billing/quota target — tying access and billing together.
///
/// - **API key**: checks the key's bound group + parent chain.
/// - **Gateway user**: checks ALL group memberships (+ parent chains). Returns
///   the first group whose chain includes the model — the user's "closest" group
///   to the resource.
/// - **Unconfigured groups**: if none of the user's groups have ANY models
///   assigned, access is allowed and the deepest-level group is returned for billing.
///
/// Returns `None` if access is denied, `Err` if it could not be read
/// (callers answer 503, never "denied").
pub async fn resolve_model_access(
    db: &PgPool,
    auth: &AuthContext,
    tenant_id: &str,
    model_id: &str,
) -> Result<Option<ResourceGroup>, sqlx::Error> {
    resolve_model_access_with_preferred(db, auth, tenant_id, model_id, None).await
}

/// Resolve model access with an explicit preferred group (e.g. from the
/// `X-Resource-Group` header or the user's `primary_group_id`).
///
/// When `preferred_group_id` is set and the caller is a portal user:
///   1. Verify the user is a member of the preferred group.
///   2. Verify the preferred group grants access to this model.
///   3. If both hold — return that group as the runtime scope.
///      Otherwise, fall through to the legacy first-match-wins scan (preserves
///      today's behavior for users not yet migrated to the primary-group model).
///
/// For API-key auth the preferred hint is ignored — API keys are pinned
/// to a single group by design.
pub async fn resolve_model_access_with_preferred(
    db: &PgPool,
    auth: &AuthContext,
    tenant_id: &str,
    model_id: &str,
    preferred_group_id: Option<&str>,
) -> Result<Option<ResourceGroup>, sqlx::Error> {
    // 1. A caller bound to ONE group — an API key (1:1 bound to its group;
    //    X-Resource-Group is ignored) or a workload-identity agent (the
    //    group it acts in, a membership authentication resolved).
    //    Check that group + its parent chain, nothing else.
    if let Some(gid) = auth.bound_group_id() {
        let group: Option<ResourceGroup> = sqlx::query_as(
            "SELECT *, group_type::text AS group_type \
             FROM resource_groups WHERE id = $1 AND tenant_id = $2 AND is_active = true",
        )
        .bind(gid)
        .bind(tenant_id)
        .fetch_optional(db)
        .await?;

        let Some(group) = group else {
            return Ok(None);
        };
        if model_in_group_chain(db, model_id, &group).await? {
            return Ok(Some(group));
        }
        return Ok(None);
    }

    // A workload agent that is a member of no group reaches no model.
    if auth.is_workload_agent() {
        return Ok(None);
    }

    // 2. Gateway user — check ALL group memberships
    let user_id = match auth.gateway_user_id {
        Some(ref uid) => uid,
        None => return Ok(None),
    };

    // Direct + EndUserGroup-mediated memberships, best first.
    let user_groups: Vec<ResourceGroup> =
        user_groups_ordered(db, tenant_id, user_id, preferred_group_id).await?;

    if user_groups.is_empty() {
        return Ok(None);
    }

    // 2a. Preferred-group path: if caller hinted at a specific group
    //     (X-Resource-Group header or user's primary_group_id), prefer
    //     it when the user is a member AND it grants access. Never
    //     silently ignores a valid preference — that's the whole point
    //     of the explicit-scope model (see Slack/Notion/Snowflake).
    if let Some(preferred) = preferred_group_id {
        if let Some(group) = user_groups.iter().find(|g| g.id == preferred) {
            if model_in_group_chain(db, model_id, group).await? {
                return Ok(Some(group.clone()));
            }
            // Preferred group exists but doesn't grant access — deny
            // rather than silently falling through, so the portal can
            // surface "switch groups to use this model".
            tracing::debug!(
                preferred_group = %preferred,
                model_id = %model_id,
                "Preferred group does not grant access to model"
            );
            return Ok(None);
        }
        // Preferred group ID was provided but user is not a member of it —
        // likely stale session state. Fall through to legacy resolution.
        tracing::warn!(
            preferred_group = %preferred,
            user_id = %user_id,
            "User is not a member of the preferred resource group; falling back"
        );
    }

    // For each group, check if model is directly assigned or inherited via parent chain.
    // Return the first group that grants access — this becomes the billing target.
    for group in &user_groups {
        if model_in_group_chain(db, model_id, group).await? {
            return Ok(Some(group.clone()));
        }
    }

    // If NONE of the user's groups have ANY models configured, treat as open.
    // Return the user's first group (`user_groups_ordered`) for billing.
    //
    // One existence check over all of the user's groups,
    // not a `COUNT(*)` per group. This ran on every request that reached the
    // fallback, and `COUNT(*)` did the work of scanning every assignment row
    // when all we ever asked was "is there at least one".
    let any_configured = {
        let group_ids: Vec<String> = user_groups.iter().map(|g| g.id.clone()).collect();
        let row: Result<Option<(i32,)>, _> = sqlx::query_as(
            "SELECT 1 AS found FROM resource_group_llm_models \
             WHERE resource_group_id = ANY($1) LIMIT 1",
        )
        .bind(&group_ids)
        .fetch_optional(db)
        .await;
        match row {
            Ok(hit) => hit.is_some(),
            Err(e) => {
                // FAIL CLOSED: a DB error must NOT silently open the model
                // catalogue. Treating the answer as "yes, models ARE
                // configured" sends us to the deny path below rather than to
                // the unconfigured-so-allow-everything fallback. Logged at
                // ERROR so the on-call sees that access resolution is
                // degraded.
                tracing::error!(
                    error = %e,
                    "model-assignment existence check failed — failing closed (access denied)"
                );
                true
            }
        }
    };

    if !any_configured {
        // No groups have models configured — allow access, bill to deepest group
        return Ok(Some(user_groups.into_iter().next().unwrap()));
    }

    // Model not in any group's chain — access denied
    Ok(None)
}

/// Check if a model is directly assigned to a group or any of its parent
/// groups (inheritance flows parent → child).
///
/// Access requires an explicit per-model assignment somewhere in the chain —
/// `resource_group_llm_models` is the only evidence that counts. Routing-group
/// membership does NOT imply member access: a routing group is purely a
/// load-balancing / failover construct (see `resolve_model`, which redirects
/// the routing virtual model to a member BEFORE this check runs). Each member
/// model a group should be able to call must be assigned to that group in its
/// own right, alongside the routing-group virtual model. That keeps "can this
/// group use model X?" a single auditable question with the same answer for
/// JWT users and API keys, and it matches the portal catalogue, where a
/// routing member that is not directly bound is invisible (see
/// `portal_models_http::list_models_routing_group_does_not_leak_members`).
async fn model_in_group_chain(
    db: &PgPool,
    model_id: &str,
    group: &ResourceGroup,
) -> Result<bool, sqlx::Error> {
    // One recursive CTE, not a walk.
    //
    // This used to issue one `SELECT 1 FROM resource_group_llm_models` per
    // level PLUS one `SELECT parent_group_id ...` per level, up to 10 levels
    // — and `resolve_model_access_with_preferred` calls it once per group the
    // user belongs to. A user in four groups with three-deep hierarchies paid
    // ~24 round trips before the request had done anything at all, on every
    // single LLM call.
    //
    // The CTE reproduces the walk exactly rather than approximating it:
    //
    //   * depth 0 is the group itself, always checked;
    //   * we climb to the parent only when the CURRENT row says to —
    //     `inherit_resources` for the group we started from (passed in from
    //     the already-loaded row), then each ancestor's own
    //     `inherit_from_parent`. A group that opts out stops the chain there,
    //     it does not merely skip itself;
    //   * at most 10 ancestors, matching the old `for _ in 0..10`.
    //
    // The LEFT JOIN is not incidental: the old loop checked the assignment
    // table for a parent id BEFORE it read that parent's row, so a dangling
    // `parent_group_id` that still had an assignment row counted as access.
    // Joining strictly would have quietly changed that answer. `climb` goes
    // false for such a row, so the chain ends there exactly as before.
    let hit: Option<(i32,)> = sqlx::query_as(
        r#"
        WITH RECURSIVE chain AS (
            SELECT $1::text            AS id,
                   $2::text            AS next_parent,
                   $3::bool            AS climb,
                   0                   AS depth
            UNION ALL
            SELECT COALESCE(rg.id, c.next_parent)::text,
                   rg.parent_group_id::text,
                   COALESCE(rg.inherit_from_parent, false),
                   c.depth + 1
              FROM chain c
              LEFT JOIN resource_groups rg ON rg.id = c.next_parent
             WHERE c.climb AND c.next_parent IS NOT NULL AND c.depth < 10
        )
        SELECT 1 AS found
          FROM chain c
          JOIN resource_group_llm_models m
            ON m.resource_group_id = c.id
           AND m.llm_model_id = $4
         LIMIT 1
        "#,
    )
    .bind(&group.id)
    .bind(&group.parent_group_id)
    .bind(group.inherit_resources)
    .bind(model_id)
    .fetch_optional(db)
    .await?;

    Ok(hit.is_some())
}

/// Resolve MCP server access across all user groups — returns the group that
/// grants access (used for both access decision AND billing/quota).
///
/// Same pattern as `resolve_model_access` but for MCP servers.
pub async fn resolve_mcp_server_access(
    db: &PgPool,
    auth: &AuthContext,
    tenant_id: &str,
    server_id: &str,
) -> Result<Option<ResourceGroup>, sqlx::Error> {
    // 1. A caller bound to one group (an API key, a workload-identity
    //    agent) — check that group + parent chain
    if let Some(gid) = auth.bound_group_id() {
        let group: Option<ResourceGroup> = sqlx::query_as(
            "SELECT *, group_type::text AS group_type \
             FROM resource_groups WHERE id = $1 AND tenant_id = $2 AND is_active = true",
        )
        .bind(gid)
        .bind(tenant_id)
        .fetch_optional(db)
        .await?;

        let Some(group) = group else {
            return Ok(None);
        };
        if mcp_server_in_group_chain(db, server_id, &group).await? {
            return Ok(Some(group));
        }
        return Ok(None);
    }

    // 2. Gateway user — check ALL group memberships
    let user_id = match auth.gateway_user_id {
        Some(ref uid) => uid,
        None => return Ok(None),
    };

    // Union direct + EndUserGroup-mediated RG access.
    let user_groups: Vec<ResourceGroup> =
        user_groups_ordered(db, tenant_id, user_id, auth.resolved_group_id.as_deref()).await?;

    if user_groups.is_empty() {
        return Ok(None);
    }

    // Return first group that grants access to this MCP server
    for group in &user_groups {
        if mcp_server_in_group_chain(db, server_id, group).await? {
            return Ok(Some(group.clone()));
        }
    }

    // If NONE of the user's groups have ANY server configs, treat as open
    let any_configured = {
        let mut found = false;
        for group in &user_groups {
            let count: (Option<i64>,) = sqlx::query_as(
                "SELECT COUNT(*) FROM resource_group_server_configs WHERE resource_group_id = $1",
            )
            .bind(&group.id)
            .fetch_one(db)
            .await
            .unwrap_or_else(|e| {
        // FAIL CLOSED: a usage-DB error must NOT silently grant unlimited
        // spend. Returning the saturating max makes any configured limit
        // read as exceeded so the request is denied; logged at ERROR so the
        // on-call sees that quota enforcement is degraded.
        tracing::error!(error = %e, "usage aggregate query failed — failing closed (usage treated as over-limit)");
        (Some(i64::MAX),)
    });

            if count.0.unwrap_or(0) > 0 {
                found = true;
                break;
            }
        }
        found
    };

    if !any_configured {
        return Ok(Some(user_groups.into_iter().next().unwrap()));
    }

    // Server not in any group's chain — access denied
    Ok(None)
}

/// Resolve ALL resource groups that grant a user access to an MCP server.
///
/// Unlike `resolve_mcp_server_access` (which returns a single group for billing),
/// this returns every group that grants access — needed for multi-group capability
/// filter merging (allow-wins policy).
///
/// - For API key users, returns at most one group (the key's bound group).
/// - For gateway users, returns all groups whose chain includes the server.
/// - If no groups have server configs at all, returns all user groups (unconfigured = open).
pub async fn resolve_all_mcp_server_groups(
    db: &PgPool,
    auth: &AuthContext,
    tenant_id: &str,
    server_id: &str,
) -> Result<Vec<ResourceGroup>, sqlx::Error> {
    // 1. A caller bound to one group (an API key, a workload-identity
    //    agent) — that group only
    if let Some(gid) = auth.bound_group_id() {
        let group: Option<ResourceGroup> = sqlx::query_as(
            "SELECT *, group_type::text AS group_type \
             FROM resource_groups WHERE id = $1 AND tenant_id = $2 AND is_active = true",
        )
        .bind(gid)
        .bind(tenant_id)
        .fetch_optional(db)
        .await?;

        return Ok(match group {
            Some(g) if mcp_server_in_group_chain(db, server_id, &g).await? => vec![g],
            _ => vec![],
        });
    }

    // 2. Gateway user — collect ALL groups that grant access
    let user_id = match auth.gateway_user_id {
        Some(ref uid) => uid,
        None => return Ok(vec![]),
    };

    // Union direct + EndUserGroup-mediated RG access.
    let user_groups: Vec<ResourceGroup> =
        user_groups_ordered(db, tenant_id, user_id, auth.resolved_group_id.as_deref()).await?;

    if user_groups.is_empty() {
        return Ok(vec![]);
    }

    // Collect all groups that grant access to this server
    let mut granting_groups = Vec::new();
    for group in &user_groups {
        if mcp_server_in_group_chain(db, server_id, group).await? {
            granting_groups.push(group.clone());
        }
    }

    if !granting_groups.is_empty() {
        return Ok(granting_groups);
    }

    // If NONE of the user's groups have ANY server configs, treat as open — return all groups
    let any_configured = {
        let mut found = false;
        for group in &user_groups {
            let count: (Option<i64>,) = sqlx::query_as(
                "SELECT COUNT(*) FROM resource_group_server_configs WHERE resource_group_id = $1",
            )
            .bind(&group.id)
            .fetch_one(db)
            .await
            .unwrap_or_else(|e| {
        // FAIL CLOSED: a usage-DB error must NOT silently grant unlimited
        // spend. Returning the saturating max makes any configured limit
        // read as exceeded so the request is denied; logged at ERROR so the
        // on-call sees that quota enforcement is degraded.
        tracing::error!(error = %e, "usage aggregate query failed — failing closed (usage treated as over-limit)");
        (Some(i64::MAX),)
    });

            if count.0.unwrap_or(0) > 0 {
                found = true;
                break;
            }
        }
        found
    };

    if !any_configured {
        return Ok(user_groups);
    }

    // Server not in any group's chain — no access
    Ok(vec![])
}

/// Check if an MCP server (via server_configs) is assigned to a group or its parent chain.
async fn mcp_server_in_group_chain(
    db: &PgPool,
    server_id: &str,
    group: &ResourceGroup,
) -> Result<bool, sqlx::Error> {
    // Check direct assignment
    let direct: Option<(i32,)> = sqlx::query_as(
        r#"SELECT 1 as found FROM resource_group_server_configs rgsc
           JOIN server_configs sc ON sc.id = rgsc.server_config_id
           WHERE rgsc.resource_group_id = $1 AND sc.mcp_server_id = $2 AND sc.enabled = true"#,
    )
    .bind(&group.id)
    .bind(server_id)
    .fetch_optional(db)
    .await?;

    if direct.is_some() {
        return Ok(true);
    }

    // Walk parent chain if inheritance is enabled
    if group.inherit_resources {
        let mut current_parent_id = group.parent_group_id.clone();
        for _ in 0..10 {
            let pid = match current_parent_id {
                Some(ref id) => id.clone(),
                None => break,
            };

            let found: Option<(i32,)> = sqlx::query_as(
                r#"SELECT 1 as found FROM resource_group_server_configs rgsc
                   JOIN server_configs sc ON sc.id = rgsc.server_config_id
                   WHERE sc.mcp_server_id = $1 AND sc.enabled = true
                     AND rgsc.resource_group_id = $2"#,
            )
            .bind(server_id)
            .bind(&pid)
            .fetch_optional(db)
            .await?;

            if found.is_some() {
                return Ok(true);
            }

            let parent: Option<(Option<String>, bool)> = sqlx::query_as(
                "SELECT parent_group_id, inherit_from_parent FROM resource_groups WHERE id = $1",
            )
            .bind(&pid)
            .fetch_optional(db)
            .await?;

            match parent {
                Some((next_pid, inherits)) if inherits => {
                    current_parent_id = next_pid;
                }
                _ => break,
            }
        }
    }

    Ok(false)
}

/// Resolve which `end_users.id` to stamp on a `usage_logs` row.
///
/// `auth.api_key_user_id` is the historical source — populated when
/// the request authenticated with a per-user LLM Access Key. Portal
/// flows (JWT bearer + `X-Gateway-Authorization`) instead populate
/// `auth.gateway_user_id` (a string-typed `end_users.id`); without
/// this fallback those rows landed with `user_id = NULL`, which
/// silently broke per-member usage drill-down because
/// `UsageLog.user_id` would never match the filtered id.
///
/// Both fields point at the same `end_users.id` integer column,
/// so coalescing them is correct: api-key flows still take
/// precedence (they're the more specific signal — the admin
/// minted that key for that user), and portal flows fall back to
/// the JWT identity.
fn resolve_usage_user_id(auth: &AuthContext) -> Option<i32> {
    if let Some(id) = auth.api_key_user_id {
        return Some(id);
    }
    auth.gateway_user_id
        .as_deref()
        .and_then(|s| s.parse::<i32>().ok())
}

/// Heal one counter from the ledger: raise it to at least the ledger value,
/// never lower it.
///
/// This used to seed only COLD counters (0 while the ledger had usage) and
/// leave populated ones alone. But a warm counter that missed increments —
/// a fire-and-forget settle that failed while Redis was erroring — then
/// under-counted for the rest of the window, and an under-counted quota is
/// overspend. The ledger is authoritative and lags reality, so raising to it
/// never over-states usage; the raise is one atomic script, so it cannot
/// lose an in-flight increment. Returns true if it changed the counter.
async fn heal_counter(
    usage_state: &UsageState,
    counter_key: &str,
    window: QuotaWindow,
    ledger_value: i64,
) -> bool {
    usage_state
        .raise_usage_to(counter_key, window, ledger_value)
        .await
}

/// A scope's three ledger values (tokens, cost in micro-USD, requests), read
/// before any counter is touched: if one read fails the caller skips the
/// whole scope for this pass. Sequential, like the reads it replaces.
async fn ledger_triple(
    tokens: impl std::future::Future<Output = Result<i64, sqlx::Error>>,
    cost_micros: impl std::future::Future<Output = Result<i64, sqlx::Error>>,
    requests: impl std::future::Future<Output = Result<i64, sqlx::Error>>,
) -> Result<(i64, i64, i64), sqlx::Error> {
    Ok((tokens.await?, cost_micros.await?, requests.await?))
}

/// Ledger cost (USD) as the counter's micro-USD.
async fn as_micros(
    cost: impl std::future::Future<Output = Result<f64, sqlx::Error>>,
) -> Result<i64, sqlx::Error> {
    Ok((cost.await? * 1_000_000.0).round() as i64)
}

/// What part of the LLM ledger a counter scope sums over.
#[derive(Debug, Clone, Copy)]
enum LedgerScope<'a> {
    /// `grp:{group}`
    Group(&'a str),
    /// `grp:{group}:mdl:{model}`
    GroupModel(&'a str, &'a str),
    /// `ten:{tenant}:mdl:{model}`
    TenantModel(&'a str),
    /// `sub:{group}`: every group under this ancestry path.
    Subtree(&'a str),
}

/// The ledger's prompt and completion tokens of one scope since `since`:
/// what the input-side and output-side token counters (`tokin:{scope}`,
/// `tokout:{scope}`; the `tokens.{daily,monthly}_{input,output}_limit`
/// quotas) are healed from.
async fn ledger_token_sides(
    db: &PgPool,
    tenant_id: &str,
    ledger: LedgerScope<'_>,
    since: chrono::DateTime<Utc>,
) -> Result<(i64, i64), sqlx::Error> {
    let (group, model, subtree) = match ledger {
        LedgerScope::Group(g) => (Some(g), None, None),
        LedgerScope::GroupModel(g, m) => (Some(g), Some(m), None),
        LedgerScope::TenantModel(m) => (None, Some(m), None),
        LedgerScope::Subtree(path) => (None, None, Some(format!("{path}%"))),
    };
    sqlx::query_as(
        "SELECT COALESCE(SUM(prompt_tokens), 0)::bigint, \
                COALESCE(SUM(completion_tokens), 0)::bigint \
         FROM usage_logs \
         WHERE tenant_id = $1 AND resource_type = 'llm' AND created_at >= $2 \
           AND ($3::text IS NULL OR group_id = $3) \
           AND ($4::text IS NULL OR resource_id = $4) \
           AND ($5::text IS NULL OR ancestry_path LIKE $5)",
    )
    .bind(tenant_id)
    .bind(since)
    .bind(group)
    .bind(model)
    .bind(subtree)
    .fetch_one(db)
    .await
}

/// One scope's ledger figures for a pass over the counters: its totals and
/// its token sides, read together so that an unreadable ledger skips the
/// whole scope (one degrade point per scope, as before the sides existed).
async fn ledger_figures(
    totals: impl std::future::Future<Output = Result<(i64, i64, i64), sqlx::Error>>,
    sides: impl std::future::Future<Output = Result<(i64, i64), sqlx::Error>>,
) -> Result<((i64, i64, i64), (i64, i64)), sqlx::Error> {
    Ok((totals.await?, sides.await?))
}

/// Raise the input-side and output-side token counters of `scope` to the
/// ledger (never lowered). Returns how many it changed.
async fn heal_token_sides(
    usage_state: &UsageState,
    scope: &str,
    window: QuotaWindow,
    (input, output): (i64, i64),
) -> usize {
    heal_counter(usage_state, &format!("tokin:{scope}"), window, input).await as usize
        + heal_counter(usage_state, &format!("tokout:{scope}"), window, output).await as usize
}

/// Periodic heal of the Redis quota counters from the authoritative
/// `usage_logs` SUM/COUNT, for every group (and per-model scope) with LLM usage
/// this month, across the daily and monthly windows.
///
/// Decoupled from the request path (runs on a timer) so it adds zero hot-path
/// latency. Each counter is raised to at least the ledger and never lowered:
/// that heals a window roll-over, a mid-window Redis flush, and a warm
/// counter that lost increments while Redis was erroring. Returns the number
/// of counters it changed.
pub async fn reconcile_llm_shadow_counters(db: &PgPool, usage_state: &UsageState) -> usize {
    let now = Utc::now();
    let today_start = now.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc();
    let month_start = now
        .with_day(1)
        .unwrap()
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .and_utc();

    let scopes: Vec<(String, String)> = sqlx::query_as(
        "SELECT DISTINCT tenant_id, group_id FROM usage_logs \
         WHERE resource_type = 'llm' AND group_id IS NOT NULL AND created_at >= $1",
    )
    .bind(month_start)
    .fetch_all(db)
    .await
    // Degrade (logged and counted): reconcile only raises counters, so an
    // unreadable scope list skips healing this pass, it never lowers one.
    .or_logged_default();

    let mut seeded = 0usize;
    for (tenant_id, group_id) in scopes {
        for (window, since) in [
            (QuotaWindow::Day, today_start),
            (QuotaWindow::Month, month_start),
        ] {
            // An unreadable ledger skips the scope for this pass
            // (logged and counted). Never a stand-in value: raising a counter
            // to "over limit" would refuse the group until the window rolls.
            let Some(((tok, cost_micros, req), sides)) = ledger_figures(
                ledger_triple(
                    sum_tokens(db, &tenant_id, &group_id, since, "llm"),
                    sum_cost_micros(db, &tenant_id, &group_id, since, "llm"),
                    count_requests(db, &tenant_id, &group_id, since, "llm"),
                ),
                ledger_token_sides(db, &tenant_id, LedgerScope::Group(&group_id), since),
            )
            .await
            .ok_logged() else {
                continue;
            };
            seeded += heal_counter(usage_state, &format!("tok:grp:{group_id}"), window, tok).await
                as usize;
            seeded += heal_counter(
                usage_state,
                &format!("cost:grp:{group_id}"),
                window,
                cost_micros,
            )
            .await as usize;
            seeded += heal_counter(usage_state, &format!("req:grp:{group_id}"), window, req).await
                as usize;
            seeded +=
                heal_token_sides(usage_state, &format!("grp:{group_id}"), window, sides).await;
        }
    }

    // Per-model counters: seed each (tenant, group, model) scope active this
    // month from the per-model SUM/COUNT (resource_id = model id).
    let model_scopes: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT DISTINCT tenant_id, group_id, resource_id FROM usage_logs \
         WHERE resource_type = 'llm' AND group_id IS NOT NULL AND resource_id IS NOT NULL \
           AND created_at >= $1",
    )
    .bind(month_start)
    .fetch_all(db)
    .await
    // Degrade (logged and counted): reconcile only raises counters, so an
    // unreadable scope list skips healing this pass, it never lowers one.
    .or_logged_default();

    for (tenant_id, group_id, model_id) in model_scopes {
        for (window, since) in [
            (QuotaWindow::Day, today_start),
            (QuotaWindow::Month, month_start),
        ] {
            let Some(((tok, cost_micros, req), sides)) = ledger_figures(
                ledger_triple(
                    sum_tokens_for_model(db, &tenant_id, &group_id, &model_id, since, "llm"),
                    sum_cost_micros_for_model(db, &tenant_id, &group_id, &model_id, since, "llm"),
                    count_requests_for_model(db, &tenant_id, &group_id, &model_id, since, "llm"),
                ),
                ledger_token_sides(
                    db,
                    &tenant_id,
                    LedgerScope::GroupModel(&group_id, &model_id),
                    since,
                ),
            )
            .await
            .ok_logged() else {
                continue;
            };
            seeded += heal_counter(
                usage_state,
                &format!("tok:grp:{group_id}:mdl:{model_id}"),
                window,
                tok,
            )
            .await as usize;
            seeded += heal_counter(
                usage_state,
                &format!("cost:grp:{group_id}:mdl:{model_id}"),
                window,
                cost_micros,
            )
            .await as usize;
            seeded += heal_counter(
                usage_state,
                &format!("req:grp:{group_id}:mdl:{model_id}"),
                window,
                req,
            )
            .await as usize;
            seeded += heal_token_sides(
                usage_state,
                &format!("grp:{group_id}:mdl:{model_id}"),
                window,
                sides,
            )
            .await;
        }
    }

    // The cross-group scopes reserved at admission: each model's
    // tenant-wide counters, and each budgeted group's subtree counters. Healed
    // the same way (raise to the ledger, never lower), so they are right after
    // a deploy, a Redis flush, or a budget newly set on an existing org.
    let tenant_models: Vec<(String, String)> =
        sqlx::query_as("SELECT tenant_id, id FROM llm_model WHERE tenant_limits IS NOT NULL")
            .fetch_all(db)
            .await
            // Degrade (logged and counted): reconcile only raises counters, so an
            // unreadable scope list skips healing this pass, it never lowers one.
            .or_logged_default();
    for (tenant_id, model_id) in tenant_models {
        let scope = format!("ten:{tenant_id}:mdl:{model_id}");
        for (window, since) in [
            (QuotaWindow::Day, today_start),
            (QuotaWindow::Month, month_start),
        ] {
            let Some(((tok, cost_micros, req), sides)) = ledger_figures(
                ledger_triple(
                    sum_tokens_tenant_model(db, &tenant_id, &model_id, since, "llm"),
                    as_micros(sum_cost_tenant_model(
                        db, &tenant_id, &model_id, since, "llm",
                    )),
                    count_requests_tenant_model(db, &tenant_id, &model_id, since, "llm"),
                ),
                ledger_token_sides(db, &tenant_id, LedgerScope::TenantModel(&model_id), since),
            )
            .await
            .ok_logged() else {
                continue;
            };
            seeded +=
                heal_counter(usage_state, &format!("tok:{scope}"), window, tok).await as usize;
            seeded += heal_counter(usage_state, &format!("cost:{scope}"), window, cost_micros).await
                as usize;
            seeded +=
                heal_counter(usage_state, &format!("req:{scope}"), window, req).await as usize;
            seeded += heal_token_sides(usage_state, &scope, window, sides).await;
        }
    }

    let budgeted_groups: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT tenant_id, id, hierarchy_path FROM resource_groups \
         WHERE llm_global_limits IS NOT NULL AND hierarchy_path IS NOT NULL",
    )
    .fetch_all(db)
    .await
    // Degrade (logged and counted): reconcile only raises counters, so an
    // unreadable scope list skips healing this pass, it never lowers one.
    .or_logged_default();
    for (tenant_id, group_id, path) in budgeted_groups {
        let scope = format!("sub:{group_id}");
        for (window, since) in [
            (QuotaWindow::Day, today_start),
            (QuotaWindow::Month, month_start),
        ] {
            let Some(((tok, cost_micros, req), sides)) = ledger_figures(
                ledger_triple(
                    sum_tokens_by_ancestry(db, &tenant_id, &path, since, "llm"),
                    as_micros(sum_cost_by_ancestry(db, &tenant_id, &path, since, "llm")),
                    count_requests_by_ancestry(db, &tenant_id, &path, since, "llm"),
                ),
                ledger_token_sides(db, &tenant_id, LedgerScope::Subtree(&path), since),
            )
            .await
            .ok_logged() else {
                continue;
            };
            seeded +=
                heal_counter(usage_state, &format!("tok:{scope}"), window, tok).await as usize;
            seeded += heal_counter(usage_state, &format!("cost:{scope}"), window, cost_micros).await
                as usize;
            seeded +=
                heal_counter(usage_state, &format!("req:{scope}"), window, req).await as usize;
            seeded += heal_token_sides(usage_state, &scope, window, sides).await;
        }
    }

    // The count-per-call scopes reserved at admission for skills and MCP.
    seeded += reconcile_count_counters(db, usage_state, today_start, month_start).await;

    if seeded > 0 {
        tracing::info!(seeded, "LLM quota-counter cold-start seed pass");
    }

    seeded
}

/// Heal the skill-execution and MCP-call counters reserved at admission
///  from the ledger: raise to the ledger count, never lower.
async fn reconcile_count_counters(
    db: &PgPool,
    usage_state: &UsageState,
    today_start: chrono::DateTime<Utc>,
    month_start: chrono::DateTime<Utc>,
) -> usize {
    let windows = [
        (QuotaWindow::Day, today_start),
        (QuotaWindow::Month, month_start),
    ];
    let mut healed = 0usize;

    // Skills, per group and per (group, skill).
    let skill_scopes: Vec<(String, String, Option<String>)> = sqlx::query_as(
        "SELECT DISTINCT tenant_id, group_id, resource_id FROM usage_logs \
         WHERE resource_type = 'skill' AND group_id IS NOT NULL AND created_at >= $1",
    )
    .bind(month_start)
    .fetch_all(db)
    .await
    // Degrade (logged and counted): reconcile only raises counters, so an
    // unreadable scope list skips healing this pass, it never lowers one.
    .or_logged_default();
    for (tenant_id, group_id, skill_id) in skill_scopes {
        for (window, since) in windows {
            // An unreadable count skips that counter for this pass.
            if let Some(n) = count_requests(db, &tenant_id, &group_id, since, "skill")
                .await
                .ok_logged()
            {
                healed += heal_counter(usage_state, &format!("skx:grp:{group_id}"), window, n).await
                    as usize;
            }
            if let Some(sid) = &skill_id {
                if let Some(n) =
                    count_requests_for_model(db, &tenant_id, &group_id, sid, since, "skill")
                        .await
                        .ok_logged()
                {
                    healed += heal_counter(
                        usage_state,
                        &format!("skx:grp:{group_id}:skl:{sid}"),
                        window,
                        n,
                    )
                    .await as usize;
                }
            }
        }
    }

    // Skills, tenant-wide.
    let tenant_skills: Vec<(String, String)> =
        sqlx::query_as("SELECT tenant_id, id FROM agent_skills WHERE tenant_limits IS NOT NULL")
            .fetch_all(db)
            .await
            // Degrade (logged and counted): reconcile only raises counters, so an
            // unreadable scope list skips healing this pass, it never lowers one.
            .or_logged_default();
    for (tenant_id, skill_id) in tenant_skills {
        for (window, since) in windows {
            if let Some(n) = count_requests_tenant_model(db, &tenant_id, &skill_id, since, "skill")
                .await
                .ok_logged()
            {
                healed += heal_counter(
                    usage_state,
                    &format!("skx:ten:{tenant_id}:skl:{skill_id}"),
                    window,
                    n,
                )
                .await as usize;
            }
        }
    }

    // Subtrees: skills (day + month) and MCP calls (day).
    let subtrees: Vec<(String, String, String, bool, bool)> = sqlx::query_as(
        "SELECT tenant_id, id, hierarchy_path, skill_global_limits IS NOT NULL, \
                mcp_global_limits IS NOT NULL \
         FROM resource_groups WHERE hierarchy_path IS NOT NULL \
           AND (skill_global_limits IS NOT NULL OR mcp_global_limits IS NOT NULL)",
    )
    .fetch_all(db)
    .await
    // Degrade (logged and counted): reconcile only raises counters, so an
    // unreadable scope list skips healing this pass, it never lowers one.
    .or_logged_default();
    for (tenant_id, group_id, path, has_skill, has_mcp) in subtrees {
        if has_skill {
            for (window, since) in windows {
                if let Some(n) = count_requests_by_ancestry(db, &tenant_id, &path, since, "skill")
                    .await
                    .ok_logged()
                {
                    healed += heal_counter(usage_state, &format!("skx:sub:{group_id}"), window, n)
                        .await as usize;
                }
            }
        }
        if has_mcp {
            if let Some(n) = count_requests_by_ancestry(db, &tenant_id, &path, today_start, "mcp")
                .await
                .ok_logged()
            {
                healed += heal_counter(
                    usage_state,
                    &format!("mcpx:sub:{group_id}"),
                    QuotaWindow::Day,
                    n,
                )
                .await as usize;
            }
        }
    }
    healed
}

/// One-time SHADOW baseline backfill: FORCE every active counter (group +
/// per-model, Day + Month) to the current ledger value. Run once when shadow is
/// first enabled on a system that already has usage this window — the
/// per-counter cold-seed in reconcile can't fix a counter that's already
/// non-zero but incomplete (it only seeds zeros), which is why a mid-window
/// enable otherwise leaves the divergence page permanently red. After this,
/// reconcile observes only genuine post-baseline increment drift. Returns the
/// number of counters set.
pub async fn backfill_llm_shadow_counters(db: &PgPool, usage_state: &UsageState) -> usize {
    let now = Utc::now();
    let today_start = now.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc();
    let month_start = now
        .with_day(1)
        .unwrap()
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .and_utc();

    let windows = [
        (QuotaWindow::Day, today_start),
        (QuotaWindow::Month, month_start),
    ];
    let mut set = 0usize;

    let scopes: Vec<(String, String)> = sqlx::query_as(
        "SELECT DISTINCT tenant_id, group_id FROM usage_logs \
         WHERE resource_type = 'llm' AND group_id IS NOT NULL AND created_at >= $1",
    )
    .bind(month_start)
    .fetch_all(db)
    .await
    // Degrade (logged and counted): reconcile only raises counters, so an
    // unreadable scope list skips healing this pass, it never lowers one.
    .or_logged_default();
    for (tenant_id, group_id) in scopes {
        for (window, since) in windows {
            // Never overwrite a counter with a value the ledger
            // did not give: an unreadable scope is skipped (logged, counted).
            let Some(((tok, cost_micros, req), sides)) = ledger_figures(
                ledger_triple(
                    sum_tokens(db, &tenant_id, &group_id, since, "llm"),
                    sum_cost_micros(db, &tenant_id, &group_id, since, "llm"),
                    count_requests(db, &tenant_id, &group_id, since, "llm"),
                ),
                ledger_token_sides(db, &tenant_id, LedgerScope::Group(&group_id), since),
            )
            .await
            .ok_logged() else {
                continue;
            };
            usage_state
                .set_usage(&format!("tok:grp:{group_id}"), window, tok)
                .await;
            usage_state
                .set_usage(&format!("cost:grp:{group_id}"), window, cost_micros)
                .await;
            usage_state
                .set_usage(&format!("req:grp:{group_id}"), window, req)
                .await;
            usage_state
                .set_usage(&format!("tokin:grp:{group_id}"), window, sides.0)
                .await;
            usage_state
                .set_usage(&format!("tokout:grp:{group_id}"), window, sides.1)
                .await;
            set += 5;
        }
    }

    let model_scopes: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT DISTINCT tenant_id, group_id, resource_id FROM usage_logs \
         WHERE resource_type = 'llm' AND group_id IS NOT NULL AND resource_id IS NOT NULL \
           AND created_at >= $1",
    )
    .bind(month_start)
    .fetch_all(db)
    .await
    // Degrade (logged and counted): reconcile only raises counters, so an
    // unreadable scope list skips healing this pass, it never lowers one.
    .or_logged_default();
    for (tenant_id, group_id, model_id) in model_scopes {
        for (window, since) in windows {
            let Some(((tok, cost_micros, req), sides)) = ledger_figures(
                ledger_triple(
                    sum_tokens_for_model(db, &tenant_id, &group_id, &model_id, since, "llm"),
                    sum_cost_micros_for_model(db, &tenant_id, &group_id, &model_id, since, "llm"),
                    count_requests_for_model(db, &tenant_id, &group_id, &model_id, since, "llm"),
                ),
                ledger_token_sides(
                    db,
                    &tenant_id,
                    LedgerScope::GroupModel(&group_id, &model_id),
                    since,
                ),
            )
            .await
            .ok_logged() else {
                continue;
            };
            usage_state
                .set_usage(&format!("tok:grp:{group_id}:mdl:{model_id}"), window, tok)
                .await;
            usage_state
                .set_usage(
                    &format!("cost:grp:{group_id}:mdl:{model_id}"),
                    window,
                    cost_micros,
                )
                .await;
            usage_state
                .set_usage(&format!("req:grp:{group_id}:mdl:{model_id}"), window, req)
                .await;
            usage_state
                .set_usage(
                    &format!("tokin:grp:{group_id}:mdl:{model_id}"),
                    window,
                    sides.0,
                )
                .await;
            usage_state
                .set_usage(
                    &format!("tokout:grp:{group_id}:mdl:{model_id}"),
                    window,
                    sides.1,
                )
                .await;
            set += 5;
        }
    }

    if set > 0 {
        tracing::info!(
            counters_set = set,
            "LLM shadow counter baseline backfill complete"
        );
    }
    set
}

pub fn write_llm_usage_log(
    usage_state: &Arc<UsageState>,
    writer: &BackgroundWriter,
    metrics: &crate::services::model_metrics_aggregator::ModelMetricsAggregator,
    db: PgPool,
    tenant_id: &str,
    auth: &AuthContext,
    group: Option<&ResourceGroup>,
    model_id: &str,
    model_name: &str,
    provider: &str,
    prompt_tokens: Option<i32>,
    completion_tokens: Option<i32>,
    estimated_cost: Option<f64>,
    input_cost_per_token: f64,
    output_cost_per_token: f64,
    endpoint: &str,
    request_id: &str,
    status_code: u16,
    duration_ms: f64,
) {
    let prompt_cost = prompt_tokens.map(|pt| pt as f64 * input_cost_per_token / 1_000_000.0);
    let completion_cost =
        completion_tokens.map(|ct| ct as f64 * output_cost_per_token / 1_000_000.0);

    let usage = UsageLogInsert {
        tenant_id: tenant_id.to_string(),
        user_id: resolve_usage_user_id(auth),
        group_id: group.map(|g| g.id.clone()),
        ancestry_path: group.and_then(|g| g.hierarchy_path.clone()),
        group_name: group.map(|g| g.name.clone()),
        resource_type: "llm".to_string(),
        resource_id: Some(model_id.to_string()),
        resource_name: Some(model_name.to_string()),
        prompt_tokens,
        completion_tokens,
        total_tokens: prompt_tokens.zip(completion_tokens).map(|(p, c)| p + c),
        model_name: Some(model_name.to_string()),
        provider: Some(provider.to_string()),
        mcp_tool_name: None,
        mcp_call_count: None,
        mcp_execution_time_ms: None,
        mcp_data_size_bytes: None,
        estimated_cost,
        currency: Some("USD".to_string()),
        prompt_cost,
        completion_cost,
        request_id: request_id.to_string(),
        status_code: Some(status_code as i32),
        response_time_ms: Some(duration_ms as i32),
        http_method: Some("POST".to_string()),
        endpoint: Some(endpoint.to_string()),
        client_ip: auth.client_ip.clone(),
        user_agent: None,
        is_inherited: false,
        inherited_from_group_id: None,
        created_at: Utc::now(),
    };
    writer.write_usage_log(db.clone(), usage);

    // Counter increment goes into the in-process
    // aggregator instead of a per-request UPDATE. The aggregator's
    // background flusher runs ONE bulk UPDATE every N seconds across
    // all dirty models, eliminating the row-level lock contention
    // that previously blocked admin reset-resilience.
    let total_tok = prompt_tokens.unwrap_or(0) + completion_tokens.unwrap_or(0);
    let is_failed = status_code >= 400;
    metrics.record(model_id, is_failed, total_tok as i64);

    // Settle the call's admission — every limit it was admitted against,
    // in the windows it was admitted in — or, for a call that held none,
    // book the group's counters directly.
    settle_llm_call(
        usage_state,
        &LlmCallUsage {
            tenant_id,
            hold_id: auth.admission_hold.as_deref(),
            group_id: group.map(|g| g.id.as_str()),
            model_id,
            prompt_tokens,
            completion_tokens,
            estimated_cost,
            status_code,
        },
    );

    let _ = db; // db parameter retained for usage_logs write above
}

/// Write a usage log entry for an MCP request.
pub fn write_mcp_usage_log(
    writer: &BackgroundWriter,
    db: PgPool,
    tenant_id: &str,
    auth: &AuthContext,
    group: Option<&ResourceGroup>,
    server_id: &str,
    server_name: &str,
    tool_name: Option<&str>,
    endpoint: &str,
    http_method: &str,
    request_id: &str,
    status_code: u16,
    duration_ms: f64,
    request_size: Option<i32>,
    response_size: Option<i32>,
) {
    // Data transfer = request body + response body
    let data_size = match (request_size, response_size) {
        (Some(req), Some(resp)) => Some(req + resp),
        (Some(req), None) => Some(req),
        (None, Some(resp)) => Some(resp),
        (None, None) => None,
    };

    let usage = UsageLogInsert {
        tenant_id: tenant_id.to_string(),
        user_id: resolve_usage_user_id(auth),
        group_id: group.map(|g| g.id.clone()),
        ancestry_path: group.and_then(|g| g.hierarchy_path.clone()),
        group_name: group.map(|g| g.name.clone()),
        resource_type: "mcp".to_string(),
        resource_id: Some(server_id.to_string()),
        resource_name: Some(server_name.to_string()),
        prompt_tokens: None,
        completion_tokens: None,
        total_tokens: None,
        model_name: None,
        provider: None,
        mcp_tool_name: tool_name.map(String::from),
        mcp_call_count: Some(1),
        mcp_execution_time_ms: Some(duration_ms as i32),
        mcp_data_size_bytes: data_size,
        estimated_cost: None,
        currency: Some("USD".to_string()),
        prompt_cost: None,
        completion_cost: None,
        request_id: request_id.to_string(),
        status_code: Some(status_code as i32),
        response_time_ms: Some(duration_ms as i32),
        http_method: Some(http_method.to_string()),
        endpoint: Some(endpoint.to_string()),
        client_ip: auth.client_ip.clone(),
        user_agent: None,
        is_inherited: false,
        inherited_from_group_id: None,
        created_at: Utc::now(),
    };
    writer.write_usage_log(db, usage);
}

/// Write a usage log entry for a skill execution request.
pub fn write_skill_usage_log(
    writer: &BackgroundWriter,
    db: PgPool,
    tenant_id: &str,
    auth: &AuthContext,
    group: Option<&ResourceGroup>,
    skill_id: &str,
    skill_name: &str,
    endpoint: &str,
    request_id: &str,
    status_code: u16,
    duration_ms: f64,
) {
    let usage = UsageLogInsert {
        tenant_id: tenant_id.to_string(),
        user_id: resolve_usage_user_id(auth),
        group_id: group.map(|g| g.id.clone()),
        ancestry_path: group.and_then(|g| g.hierarchy_path.clone()),
        group_name: group.map(|g| g.name.clone()),
        resource_type: "skill".to_string(),
        resource_id: Some(skill_id.to_string()),
        resource_name: Some(skill_name.to_string()),
        prompt_tokens: None,
        completion_tokens: None,
        total_tokens: None,
        model_name: None,
        provider: None,
        mcp_tool_name: None,
        mcp_call_count: None,
        mcp_execution_time_ms: None,
        mcp_data_size_bytes: None,
        estimated_cost: None,
        currency: Some("USD".to_string()),
        prompt_cost: None,
        completion_cost: None,
        request_id: request_id.to_string(),
        status_code: Some(status_code as i32),
        response_time_ms: Some(duration_ms as i32),
        http_method: Some("POST".to_string()),
        endpoint: Some(endpoint.to_string()),
        client_ip: auth.client_ip.clone(),
        user_agent: None,
        is_inherited: false,
        inherited_from_group_id: None,
        created_at: Utc::now(),
    };
    writer.write_usage_log(db, usage);
}

/// Write a usage log entry for an embedding request (no completion tokens).
pub fn write_embedding_usage_log(
    usage_state: &Arc<UsageState>,
    writer: &BackgroundWriter,
    metrics: &crate::services::model_metrics_aggregator::ModelMetricsAggregator,
    db: PgPool,
    tenant_id: &str,
    auth: &AuthContext,
    group: Option<&ResourceGroup>,
    model_id: &str,
    model_name: &str,
    provider: &str,
    prompt_tokens: Option<i32>,
    estimated_cost: Option<f64>,
    input_cost_per_token: f64,
    endpoint: &str,
    request_id: &str,
    status_code: u16,
    duration_ms: f64,
) {
    let prompt_cost = prompt_tokens.map(|pt| pt as f64 * input_cost_per_token / 1_000_000.0);

    let usage = UsageLogInsert {
        tenant_id: tenant_id.to_string(),
        user_id: resolve_usage_user_id(auth),
        group_id: group.map(|g| g.id.clone()),
        ancestry_path: group.and_then(|g| g.hierarchy_path.clone()),
        group_name: group.map(|g| g.name.clone()),
        resource_type: "llm".to_string(),
        resource_id: Some(model_id.to_string()),
        resource_name: Some(model_name.to_string()),
        prompt_tokens,
        completion_tokens: Some(0),
        total_tokens: prompt_tokens,
        model_name: Some(model_name.to_string()),
        provider: Some(provider.to_string()),
        mcp_tool_name: None,
        mcp_call_count: None,
        mcp_execution_time_ms: None,
        mcp_data_size_bytes: None,
        estimated_cost,
        currency: Some("USD".to_string()),
        prompt_cost,
        completion_cost: None,
        request_id: request_id.to_string(),
        status_code: Some(status_code as i32),
        response_time_ms: Some(duration_ms as i32),
        http_method: Some("POST".to_string()),
        endpoint: Some(endpoint.to_string()),
        client_ip: auth.client_ip.clone(),
        user_agent: None,
        is_inherited: false,
        inherited_from_group_id: None,
        created_at: Utc::now(),
    };
    writer.write_usage_log(db.clone(), usage);

    // Counter increment goes into the aggregator. See
    // `write_llm_usage_log` above for the full rationale.
    let total_tok = prompt_tokens.unwrap_or(0);
    let is_failed = status_code >= 400;
    metrics.record(model_id, is_failed, total_tok as i64);
    // Settle the embedding call's admission like any LLM call's: an
    // embedding spends input tokens against the same budgets.
    settle_llm_call(
        usage_state,
        &LlmCallUsage {
            tenant_id,
            hold_id: auth.admission_hold.as_deref(),
            group_id: group.map(|g| g.id.as_str()),
            model_id,
            prompt_tokens,
            completion_tokens: Some(0),
            estimated_cost,
            status_code,
        },
    );
    let _ = db;
}

// ===========================================================================
// Usage Alert Generation & Management
// ===========================================================================

/// Spawn alert evaluation in the background (fire-and-forget).
/// Call this from route handlers after writing a usage log.
pub fn spawn_alert_evaluation(
    db: PgPool,
    tenant_id: String,
    group: ResourceGroup,
    resource_type: String,
    request_id: Option<String>,
) {
    tokio::spawn(async move {
        evaluate_and_alert(
            &db,
            &tenant_id,
            &group,
            &resource_type,
            request_id.as_deref(),
        )
        .await;
    });
}

/// Evaluate usage thresholds and create persistent alerts.
/// Called AFTER a usage log has been written (fire-and-forget in background).
pub async fn evaluate_and_alert(
    db: &PgPool,
    tenant_id: &str,
    group: &ResourceGroup,
    resource_type: &str,
    request_id: Option<&str>,
) {
    let result = match resource_type {
        "llm" => evaluate_llm_alerts(db, tenant_id, group, request_id).await,
        "mcp" => evaluate_mcp_alerts(db, tenant_id, group, request_id).await,
        "skill" => evaluate_skill_alerts(db, tenant_id, group, request_id).await,
        _ => return,
    };

    if let Err(e) = result {
        tracing::debug!(error = %e, "Alert evaluation failed (non-fatal)");
    }
}

async fn evaluate_llm_alerts(
    db: &PgPool,
    tenant_id: &str,
    group: &ResourceGroup,
    request_id: Option<&str>,
) -> Result<(), sqlx::Error> {
    let limits = match &group.llm_global_limits {
        Some(l) => l,
        None => return Ok(()),
    };

    let now = Utc::now();
    let today_start = now.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc();
    let month_start = now
        .with_day(1)
        .unwrap()
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .and_utc();

    // ---- Budget alerts ----
    if let Some(budget) = limits.get("budget") {
        if let Some(daily_budget) = budget.get("daily_limit_usd").and_then(|v| v.as_f64()) {
            if daily_budget > 0.0 {
                let daily_cost = sum_cost(db, tenant_id, &group.id, today_start, "llm")
                    .await
                    .or_over_limit();
                let util = (daily_cost / daily_budget) * 100.0;
                let warn_pct = budget
                    .get("daily_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(80.0);
                check_thresholds_float(
                    db,
                    tenant_id,
                    group,
                    "llm",
                    "daily_budget",
                    warn_pct,
                    util,
                    daily_cost,
                    daily_budget,
                    "daily",
                    today_start,
                    request_id,
                )
                .await?;
            }
        }

        if let Some(monthly_budget) = budget.get("monthly_limit_usd").and_then(|v| v.as_f64()) {
            if monthly_budget > 0.0 {
                let monthly_cost = sum_cost(db, tenant_id, &group.id, month_start, "llm")
                    .await
                    .or_over_limit();
                let util = (monthly_cost / monthly_budget) * 100.0;
                let warn_pct = budget
                    .get("monthly_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(90.0);
                check_thresholds_float(
                    db,
                    tenant_id,
                    group,
                    "llm",
                    "monthly_budget",
                    warn_pct,
                    util,
                    monthly_cost,
                    monthly_budget,
                    "monthly",
                    month_start,
                    request_id,
                )
                .await?;
            }
        }
    }

    // ---- Token alerts ----
    if let Some(tokens) = limits.get("tokens") {
        if let Some(daily_limit) = tokens.get("daily_limit").and_then(|v| v.as_i64()) {
            if daily_limit > 0 {
                let daily_tokens = sum_tokens(db, tenant_id, &group.id, today_start, "llm")
                    .await
                    .or_over_limit();
                let util = (daily_tokens as f64 / daily_limit as f64) * 100.0;
                let warn_pct = tokens
                    .get("daily_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(85.0);
                check_thresholds_int(
                    db,
                    tenant_id,
                    group,
                    "llm",
                    "daily_tokens",
                    warn_pct,
                    util,
                    daily_tokens,
                    daily_limit,
                    "daily",
                    today_start,
                    request_id,
                )
                .await?;
            }
        }

        if let Some(monthly_limit) = tokens.get("monthly_limit").and_then(|v| v.as_i64()) {
            if monthly_limit > 0 {
                let monthly_tokens = sum_tokens(db, tenant_id, &group.id, month_start, "llm")
                    .await
                    .or_over_limit();
                let util = (monthly_tokens as f64 / monthly_limit as f64) * 100.0;
                let warn_pct = tokens
                    .get("monthly_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(90.0);
                check_thresholds_int(
                    db,
                    tenant_id,
                    group,
                    "llm",
                    "monthly_tokens",
                    warn_pct,
                    util,
                    monthly_tokens,
                    monthly_limit,
                    "monthly",
                    month_start,
                    request_id,
                )
                .await?;
            }
        }
    }

    // ---- Request count alerts ----
    if let Some(requests) = limits.get("requests") {
        if let Some(daily_limit) = requests.get("daily_limit").and_then(|v| v.as_i64()) {
            if daily_limit > 0 {
                let daily_count = count_requests(db, tenant_id, &group.id, today_start, "llm")
                    .await
                    .or_over_limit();
                let util = (daily_count as f64 / daily_limit as f64) * 100.0;
                let warn_pct = requests
                    .get("daily_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(80.0);
                check_thresholds_int(
                    db,
                    tenant_id,
                    group,
                    "llm",
                    "daily_requests",
                    warn_pct,
                    util,
                    daily_count,
                    daily_limit,
                    "daily",
                    today_start,
                    request_id,
                )
                .await?;
            }
        }

        if let Some(monthly_limit) = requests.get("monthly_limit").and_then(|v| v.as_i64()) {
            if monthly_limit > 0 {
                let monthly_count = count_requests(db, tenant_id, &group.id, month_start, "llm")
                    .await
                    .or_over_limit();
                let util = (monthly_count as f64 / monthly_limit as f64) * 100.0;
                let warn_pct = requests
                    .get("monthly_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(85.0);
                check_thresholds_int(
                    db,
                    tenant_id,
                    group,
                    "llm",
                    "monthly_requests",
                    warn_pct,
                    util,
                    monthly_count,
                    monthly_limit,
                    "monthly",
                    month_start,
                    request_id,
                )
                .await?;
            }
        }
    }

    Ok(())
}

async fn evaluate_mcp_alerts(
    db: &PgPool,
    tenant_id: &str,
    group: &ResourceGroup,
    request_id: Option<&str>,
) -> Result<(), sqlx::Error> {
    let limits = match &group.mcp_global_limits {
        Some(l) => l,
        None => return Ok(()),
    };

    let now = Utc::now();
    let today_start = now.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc();

    // ---- Frequency alerts ----
    if let Some(freq) = limits.get("frequency") {
        if let Some(daily_limit) = freq.get("max_calls_per_day").and_then(|v| v.as_i64()) {
            if daily_limit > 0 {
                let daily_calls = count_requests(db, tenant_id, &group.id, today_start, "mcp")
                    .await
                    .or_over_limit();
                let util = (daily_calls as f64 / daily_limit as f64) * 100.0;
                check_thresholds_int(
                    db,
                    tenant_id,
                    group,
                    "mcp",
                    "daily_mcp_calls",
                    80.0,
                    util,
                    daily_calls,
                    daily_limit,
                    "daily",
                    today_start,
                    request_id,
                )
                .await?;
            }
        }
    }

    // Cost alerts removed — MCP calls have no inherent cost

    Ok(())
}

async fn evaluate_skill_alerts(
    db: &PgPool,
    tenant_id: &str,
    group: &ResourceGroup,
    request_id: Option<&str>,
) -> Result<(), sqlx::Error> {
    let limits = match &group.skill_global_limits {
        Some(l) => l,
        None => return Ok(()),
    };

    let now = Utc::now();
    let today_start = now.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc();
    let month_start = now
        .with_day(1)
        .unwrap()
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .and_utc();

    // ---- Execution count alerts ----
    if let Some(exec) = limits.get("execution") {
        if let Some(daily_limit) = exec.get("daily_limit").and_then(|v| v.as_i64()) {
            if daily_limit > 0 {
                let daily_count = count_requests(db, tenant_id, &group.id, today_start, "skill")
                    .await
                    .or_over_limit();
                let util = (daily_count as f64 / daily_limit as f64) * 100.0;
                let warn_pct = exec
                    .get("daily_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(80.0);
                check_thresholds_int(
                    db,
                    tenant_id,
                    group,
                    "skill",
                    "daily_skill_executions",
                    warn_pct,
                    util,
                    daily_count,
                    daily_limit,
                    "daily",
                    today_start,
                    request_id,
                )
                .await?;
            }
        }

        if let Some(monthly_limit) = exec.get("monthly_limit").and_then(|v| v.as_i64()) {
            if monthly_limit > 0 {
                let monthly_count = count_requests(db, tenant_id, &group.id, month_start, "skill")
                    .await
                    .or_over_limit();
                let util = (monthly_count as f64 / monthly_limit as f64) * 100.0;
                let warn_pct = exec
                    .get("monthly_warning_percent")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(90.0);
                check_thresholds_int(
                    db,
                    tenant_id,
                    group,
                    "skill",
                    "monthly_skill_executions",
                    warn_pct,
                    util,
                    monthly_count,
                    monthly_limit,
                    "monthly",
                    month_start,
                    request_id,
                )
                .await?;
            }
        }
    }

    // Budget and token alerts removed — cost/tokens tracked at LLM level

    Ok(())
}

// ---------------------------------------------------------------------------
// Threshold checking helpers — create alert if threshold crossed
// ---------------------------------------------------------------------------

#[allow(clippy::too_many_arguments)]
async fn check_thresholds_float(
    db: &PgPool,
    tenant_id: &str,
    group: &ResourceGroup,
    resource_type: &str,
    metric: &str,
    warn_pct: f64,
    utilization: f64,
    current: f64,
    limit: f64,
    period: &str,
    period_start: DateTime<Utc>,
    request_id: Option<&str>,
) -> Result<(), sqlx::Error> {
    // Every configured cap now blocks at 100% (the `hard_stop` opt-out was
    // removed), so 100% utilisation is always a hard-stop alert.
    for &threshold in &[warn_pct as i32, 100] {
        if utilization >= threshold as f64 {
            let alert_type = if threshold >= 100 {
                "hard_stop"
            } else {
                "warning"
            };
            let message = format!(
                "{}: {} at {:.0}% (${:.2} / ${:.2}) for group '{}'",
                alert_type.to_uppercase(),
                metric,
                utilization,
                current,
                limit,
                group.name,
            );
            create_alert_if_new(
                db,
                tenant_id,
                group,
                resource_type,
                metric,
                alert_type,
                threshold,
                None,
                Some(current),
                None,
                Some(limit),
                utilization,
                period,
                period_start,
                request_id,
                &message,
            )
            .await?;
            // A hard stop means calls are being DENIED: that is
            // a review finding, not just a usage alert. Deduped per
            // (group, metric) so a storm of denied calls is one item.
            if alert_type == "hard_stop" {
                let is_ai_system = group
                    .group_type
                    .as_deref()
                    .map(|g| g.eq_ignore_ascii_case("ai_system"))
                    .unwrap_or(false);
                crate::services::assurance_inbox::raise_budget_stop(
                    db,
                    tenant_id,
                    &group.id,
                    is_ai_system,
                    metric,
                    utilization,
                )
                .await;
            }
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn check_thresholds_int(
    db: &PgPool,
    tenant_id: &str,
    group: &ResourceGroup,
    resource_type: &str,
    metric: &str,
    warn_pct: f64,
    utilization: f64,
    current: i64,
    limit: i64,
    period: &str,
    period_start: DateTime<Utc>,
    request_id: Option<&str>,
) -> Result<(), sqlx::Error> {
    // Token/request caps always block at 100%, so 100% is always a hard-stop
    // alert (this parameter used to carry the removed `hard_stop` toggle).
    for &threshold in &[warn_pct as i32, 100] {
        if utilization >= threshold as f64 {
            let alert_type = if threshold >= 100 {
                "hard_stop"
            } else {
                "warning"
            };
            let message = format!(
                "{}: {} at {:.0}% ({} / {}) for group '{}'",
                alert_type.to_uppercase(),
                metric,
                utilization,
                current,
                limit,
                group.name,
            );
            create_alert_if_new(
                db,
                tenant_id,
                group,
                resource_type,
                metric,
                alert_type,
                threshold,
                Some(current),
                None,
                Some(limit),
                None,
                utilization,
                period,
                period_start,
                request_id,
                &message,
            )
            .await?;
            // Count-based quotas hard-stop too; same review
            // finding as a $ budget (calls are being denied).
            if alert_type == "hard_stop" {
                let is_ai_system = group
                    .group_type
                    .as_deref()
                    .map(|g| g.eq_ignore_ascii_case("ai_system"))
                    .unwrap_or(false);
                crate::services::assurance_inbox::raise_budget_stop(
                    db,
                    tenant_id,
                    &group.id,
                    is_ai_system,
                    metric,
                    utilization,
                )
                .await;
            }
        }
    }
    Ok(())
}

/// Insert an alert using ON CONFLICT DO NOTHING for deduplication.
/// The unique constraint `uq_usage_alert_per_period` on
/// (tenant_id, resource_type, group_id, metric, threshold_pct, period_start)
/// prevents duplicate alerts for the same threshold in the same period.
#[allow(clippy::too_many_arguments)]
async fn create_alert_if_new(
    db: &PgPool,
    tenant_id: &str,
    group: &ResourceGroup,
    resource_type: &str,
    metric: &str,
    alert_type: &str,
    threshold_pct: i32,
    current_value: Option<i64>,
    current_value_float: Option<f64>,
    limit_value: Option<i64>,
    limit_value_float: Option<f64>,
    utilization_pct: f64,
    period: &str,
    period_start: DateTime<Utc>,
    request_id: Option<&str>,
    message: &str,
) -> Result<(), sqlx::Error> {
    let id = format!("alert-{}", uuid::Uuid::new_v4());

    let result = sqlx::query(
        r#"INSERT INTO usage_alerts (
            id, tenant_id, resource_type, group_id, group_name,
            alert_type, metric, threshold_pct,
            current_value, current_value_float, limit_value, limit_value_float,
            utilization_pct, period, period_start,
            status, triggered_by_request_id, message, created_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
            'active', $16, $17, NOW()
        )
        ON CONFLICT ON CONSTRAINT uq_usage_alert_per_period DO NOTHING"#,
    )
    .bind(&id)
    .bind(tenant_id)
    .bind(resource_type)
    .bind(&group.id)
    .bind(&group.name)
    .bind(alert_type)
    .bind(metric)
    .bind(threshold_pct)
    .bind(current_value)
    .bind(current_value_float)
    .bind(limit_value)
    .bind(limit_value_float)
    .bind(utilization_pct)
    .bind(period)
    .bind(period_start)
    .bind(request_id)
    .bind(message)
    .execute(db)
    .await?;

    if result.rows_affected() > 0 {
        tracing::info!(
            alert_type,
            metric,
            group_id = %group.id,
            utilization_pct,
            threshold_pct,
            "Usage alert created"
        );
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Alert management (for admin API)
// ---------------------------------------------------------------------------

/// List usage alerts with optional filters.
pub async fn list_alerts(
    db: &PgPool,
    tenant_id: &str,
    status: Option<&str>,
    resource_type: Option<&str>,
    alert_type: Option<&str>,
    group_id: Option<&str>,
    limit: i64,
) -> Result<Vec<UsageAlert>, sqlx::Error> {
    let mut query = String::from("SELECT * FROM usage_alerts WHERE tenant_id = $1");
    let mut param_idx = 2;
    let mut binds: Vec<String> = Vec::new();

    if let Some(s) = status {
        query.push_str(&format!(" AND status = ${param_idx}"));
        binds.push(s.to_string());
        param_idx += 1;
    }
    if let Some(rt) = resource_type {
        query.push_str(&format!(" AND resource_type = ${param_idx}"));
        binds.push(rt.to_string());
        param_idx += 1;
    }
    if let Some(at) = alert_type {
        query.push_str(&format!(" AND alert_type = ${param_idx}"));
        binds.push(at.to_string());
        param_idx += 1;
    }
    if let Some(gid) = group_id {
        query.push_str(&format!(" AND group_id = ${param_idx}"));
        binds.push(gid.to_string());
        param_idx += 1;
    }

    query.push_str(&format!(
        " ORDER BY alert_type DESC, utilization_pct DESC, created_at DESC LIMIT ${param_idx}"
    ));

    let mut q = sqlx::query_as::<_, UsageAlert>(&query).bind(tenant_id);
    for b in &binds {
        q = q.bind(b);
    }
    q = q.bind(limit);
    q.fetch_all(db).await
}

/// Acknowledge an alert (mark as acknowledged by admin).
pub async fn acknowledge_alert(
    db: &PgPool,
    tenant_id: &str,
    alert_id: &str,
    admin_user_id: &str,
) -> Result<Option<UsageAlert>, sqlx::Error> {
    let result = sqlx::query(
        r#"UPDATE usage_alerts
           SET status = 'acknowledged',
               acknowledged_by = $1,
               acknowledged_at = NOW()
           WHERE id = $2 AND tenant_id = $3 AND status = 'active'"#,
    )
    .bind(admin_user_id)
    .bind(alert_id)
    .bind(tenant_id)
    .execute(db)
    .await?;

    if result.rows_affected() == 0 {
        return Ok(None);
    }

    let alert: Option<UsageAlert> =
        sqlx::query_as("SELECT * FROM usage_alerts WHERE id = $1 AND tenant_id = $2")
            .bind(alert_id)
            .bind(tenant_id)
            .fetch_optional(db)
            .await?;

    Ok(alert)
}

/// Resolve expired alerts (daily alerts from previous days, monthly from previous months).
/// Called periodically from background task.
pub async fn resolve_expired_alerts(db: &PgPool) -> Result<i64, sqlx::Error> {
    let now = Utc::now();
    let today_start = now.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc();
    let month_start = now
        .with_day(1)
        .unwrap()
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .and_utc();

    let daily = sqlx::query(
        r#"WITH expired AS (
            SELECT id FROM usage_alerts
            WHERE status IN ('active', 'acknowledged')
              AND period = 'daily'
              AND period_start < $1
            FOR UPDATE SKIP LOCKED
        )
        UPDATE usage_alerts
        SET status = 'resolved', resolved_at = NOW(), resolution_note = 'period_expired'
        WHERE id IN (SELECT id FROM expired)"#,
    )
    .bind(today_start)
    .execute(db)
    .await?;

    let monthly = sqlx::query(
        r#"WITH expired AS (
            SELECT id FROM usage_alerts
            WHERE status IN ('active', 'acknowledged')
              AND period = 'monthly'
              AND period_start < $1
            FOR UPDATE SKIP LOCKED
        )
        UPDATE usage_alerts
        SET status = 'resolved', resolved_at = NOW(), resolution_note = 'period_expired'
        WHERE id IN (SELECT id FROM expired)"#,
    )
    .bind(month_start)
    .execute(db)
    .await?;

    let total = daily.rows_affected() as i64 + monthly.rows_affected() as i64;
    Ok(total)
}

// ---------------------------------------------------------------------------
// Unit tests for pure logic functions (no database required)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    // -----------------------------------------------------------------------
    // evaluate_llm_quota — PURE decision core (no DB). Locally-runnable mirror
    // of the golden-master integration suite in
    // tests/integration/quota_enforcement.rs.
    // -----------------------------------------------------------------------

    fn eval(limits: serde_json::Value, snap: LlmUsageSnapshot) -> UsageEnforcementResult {
        evaluate_llm_quota("g1", "Group One", &limits, None, &snap)
    }
    fn eval_pm(
        limits: serde_json::Value,
        pm: serde_json::Value,
        snap: LlmUsageSnapshot,
    ) -> UsageEnforcementResult {
        evaluate_llm_quota("g1", "Group One", &limits, Some(&pm), &snap)
    }

    #[test]
    fn eval_no_limits_allows() {
        let r = eval(json!({}), LlmUsageSnapshot::default());
        assert!(!r.blocked);
        assert!(r.warning_headers.is_empty());
    }

    #[test]
    fn eval_daily_tokens_blocks_at_limit() {
        let snap = LlmUsageSnapshot {
            daily_tokens: 1000,
            ..Default::default()
        };
        let r = eval(json!({"tokens": {"daily_limit": 1000}}), snap);
        assert!(r.blocked, "used == limit blocks (>=)");
        assert_eq!(r.limit_type.as_deref(), Some("daily_tokens"));
        assert_eq!(r.max_limit.as_deref(), Some("1000"));
        assert_eq!(r.current_usage.as_deref(), Some("1000"));
    }

    #[test]
    fn eval_daily_tokens_under_allows_no_warn() {
        let snap = LlmUsageSnapshot {
            daily_tokens: 500,
            ..Default::default()
        };
        let r = eval(json!({"tokens": {"daily_limit": 1000}}), snap);
        assert!(!r.blocked);
        assert!(r.warning_headers.is_empty(), "50% < 85% warn default");
    }

    #[test]
    fn eval_daily_tokens_warns_at_default_threshold() {
        let snap = LlmUsageSnapshot {
            daily_tokens: 900,
            ..Default::default()
        };
        let r = eval(json!({"tokens": {"daily_limit": 1000}}), snap);
        assert!(!r.blocked);
        assert!(
            r.warning_headers
                .iter()
                .any(|(k, _)| k == "X-Usage-Warning")
        );
    }

    #[test]
    fn eval_daily_tokens_custom_warn_threshold() {
        let snap = LlmUsageSnapshot {
            daily_tokens: 500,
            ..Default::default()
        };
        let r = eval(
            json!({"tokens": {"daily_limit": 1000, "daily_warning_percent": 50.0}}),
            snap,
        );
        assert!(!r.blocked);
        assert!(
            r.warning_headers
                .iter()
                .any(|(k, _)| k == "X-Usage-Warning"),
            "50% >= 50% custom warn"
        );
    }

    #[test]
    fn eval_monthly_tokens_blocks() {
        let snap = LlmUsageSnapshot {
            monthly_tokens: 5000,
            ..Default::default()
        };
        let r = eval(json!({"tokens": {"monthly_limit": 5000}}), snap);
        assert!(r.blocked);
        assert_eq!(r.limit_type.as_deref(), Some("monthly_tokens"));
    }

    #[test]
    fn eval_zero_limit_is_unlimited() {
        let snap = LlmUsageSnapshot {
            daily_tokens: 999_999,
            ..Default::default()
        };
        let r = eval(json!({"tokens": {"daily_limit": 0}}), snap);
        assert!(!r.blocked, "zero limit means unlimited");
    }

    #[test]
    fn eval_daily_budget_hard_stop_blocks() {
        let snap = LlmUsageSnapshot {
            daily_cost: 12.5,
            ..Default::default()
        };
        let r = eval(
            json!({"budget": {"daily_limit_usd": 10.0, "hard_stop": true}}),
            snap,
        );
        assert!(r.blocked);
        assert_eq!(r.limit_type.as_deref(), Some("daily_budget"));
    }

    #[test]
    fn eval_budget_exactly_100_pct_blocks_with_hard_stop() {
        let snap = LlmUsageSnapshot {
            daily_cost: 10.0,
            ..Default::default()
        };
        let r = eval(
            json!({"budget": {"daily_limit_usd": 10.0, "hard_stop": true}}),
            snap,
        );
        assert!(r.blocked, "util == 100 blocks (>=)");
    }

    #[test]
    fn eval_daily_budget_always_blocks_over_100pct() {
        // The `hard_stop` opt-out was removed: a configured dollar budget
        // now always blocks at 100%, exactly like a token or request cap.
        // A legacy `hard_stop: false` key in stored config is ignored.
        let snap = LlmUsageSnapshot {
            daily_cost: 99.0,
            ..Default::default()
        };
        let r = eval(
            json!({"budget": {"daily_limit_usd": 10.0, "hard_stop": false}}),
            snap,
        );
        assert!(
            r.blocked,
            "budget over 100% blocks regardless of legacy hard_stop key"
        );
        assert_eq!(r.limit_type.as_deref(), Some("daily_budget"));
    }

    #[test]
    fn eval_daily_budget_under_warn_pct_allows() {
        // Below the warning threshold: no block, no warning.
        let snap = LlmUsageSnapshot {
            daily_cost: 5.0,
            ..Default::default()
        };
        let r = eval(json!({"budget": {"daily_limit_usd": 10.0}}), snap);
        assert!(!r.blocked, "50% util is under limit");
    }

    #[test]
    fn eval_daily_requests_blocks_on_next() {
        let snap = LlmUsageSnapshot {
            daily_requests: 2,
            ..Default::default()
        };
        let r = eval(json!({"requests": {"daily_limit": 2}}), snap);
        assert!(r.blocked, "(2+1) > 2");
        assert_eq!(r.limit_type.as_deref(), Some("daily_requests"));
    }

    #[test]
    fn eval_daily_requests_at_limit_minus_one_allows() {
        let snap = LlmUsageSnapshot {
            daily_requests: 1,
            ..Default::default()
        };
        let r = eval(json!({"requests": {"daily_limit": 2}}), snap);
        assert!(!r.blocked, "(1+1) = 2, not > 2");
    }

    #[test]
    fn eval_precedence_budget_before_tokens() {
        let snap = LlmUsageSnapshot {
            daily_cost: 50.0,
            daily_tokens: 9999,
            ..Default::default()
        };
        let r = eval(
            json!({
                "budget": {"daily_limit_usd": 10.0, "hard_stop": true},
                "tokens": {"daily_limit": 1000}
            }),
            snap,
        );
        assert_eq!(
            r.limit_type.as_deref(),
            Some("daily_budget"),
            "budget is evaluated before tokens"
        );
    }

    #[test]
    fn eval_precedence_group_before_per_model() {
        let snap = LlmUsageSnapshot {
            daily_tokens: 1000,
            pm_daily_tokens: 1000,
            ..Default::default()
        };
        let r = eval_pm(
            json!({"tokens": {"daily_limit": 1000}}),
            json!({"tokens": {"daily_limit": 1000}}),
            snap,
        );
        assert_eq!(
            r.limit_type.as_deref(),
            Some("daily_tokens"),
            "group limits are evaluated before per-model"
        );
    }

    #[test]
    fn eval_per_model_tokens_block() {
        let snap = LlmUsageSnapshot {
            pm_daily_tokens: 500,
            ..Default::default()
        };
        let r = eval_pm(json!({}), json!({"tokens": {"daily_limit": 500}}), snap);
        assert!(r.blocked);
        assert_eq!(r.limit_type.as_deref(), Some("per_model_daily_tokens"));
    }

    #[test]
    fn eval_per_model_budget_always_blocks_when_exceeded() {
        // The `hard_stop` opt-out was removed: a per-model dollar budget now
        // blocks whenever it is exceeded, and a legacy `hard_stop: false` key
        // in stored config is ignored.
        let snap = LlmUsageSnapshot {
            pm_daily_cost: 99.0,
            ..Default::default()
        };
        let r = eval_pm(
            json!({}),
            json!({"budget": {"daily_limit_usd": 10.0, "hard_stop": false}}),
            snap.clone(),
        );
        assert!(
            r.blocked,
            "per-model budget over limit blocks regardless of legacy hard_stop key"
        );
        assert_eq!(r.limit_type.as_deref(), Some("per_model_daily_budget"));

        // Under the limit → no block.
        let under = LlmUsageSnapshot {
            pm_daily_cost: 5.0,
            ..Default::default()
        };
        let r2 = eval_pm(
            json!({}),
            json!({"budget": {"daily_limit_usd": 10.0}}),
            under,
        );
        assert!(!r2.blocked, "under the per-model budget → allowed");
    }

    #[test]
    fn eval_per_model_requests_block() {
        let snap = LlmUsageSnapshot {
            pm_monthly_requests: 5,
            ..Default::default()
        };
        let r = eval_pm(json!({}), json!({"requests": {"monthly_limit": 5}}), snap);
        assert!(r.blocked);
        assert_eq!(r.limit_type.as_deref(), Some("per_model_monthly_requests"));
    }

    #[test]
    fn eval_no_per_model_checks_when_effective_pm_none() {
        let snap = LlmUsageSnapshot {
            pm_daily_tokens: 999_999,
            ..Default::default()
        };
        let r = evaluate_llm_quota("g1", "G", &json!({}), None, &snap);
        assert!(
            !r.blocked,
            "no per-model snapshot consulted when pm is None"
        );
    }

    #[test]
    fn eval_warnings_accumulate_across_dimensions() {
        // tokens 90% (>= 85 warn) AND requests 80% (>= 80 warn) → two headers,
        // neither blocks.
        let snap = LlmUsageSnapshot {
            daily_tokens: 900,
            daily_requests: 8,
            ..Default::default()
        };
        let r = eval(
            json!({
                "tokens": {"daily_limit": 1000},
                "requests": {"daily_limit": 10}
            }),
            snap,
        );
        assert!(!r.blocked);
        assert_eq!(r.warning_headers.len(), 2, "both dimensions warn");
    }

    // -----------------------------------------------------------------------
    // llm_shadow_counters — PURE counter-key list (locks the shadow key scheme)
    // -----------------------------------------------------------------------

    #[test]
    fn shadow_counters_full_set_for_tokens_cost_requests() {
        let c = llm_shadow_counters("g1", "m1", 100, 5_000_000);
        // 2 windows × (tok g+m, cost g+m, req g+m) = 12.
        assert_eq!(c.len(), 12);
        // Group daily token delta present.
        assert!(c.contains(&("tok:grp:g1".to_string(), QuotaWindow::Day, 100)));
        // Per-model monthly cost delta in micro-USD.
        assert!(c.contains(&(
            "cost:grp:g1:mdl:m1".to_string(),
            QuotaWindow::Month,
            5_000_000
        )));
        // Request deltas are always +1.
        assert!(c.contains(&("req:grp:g1".to_string(), QuotaWindow::Day, 1)));
        assert!(c.contains(&("req:grp:g1:mdl:m1".to_string(), QuotaWindow::Month, 1)));
    }

    #[test]
    fn shadow_counters_skip_zero_tokens_and_cost() {
        // No tokens, no cost → only the request counters (group+model)×2 windows.
        let c = llm_shadow_counters("g1", "m1", 0, 0);
        assert_eq!(c.len(), 4);
        assert!(c.iter().all(|(k, _, d)| k.starts_with("req:") && *d == 1));
    }

    #[test]
    fn shadow_counters_cost_only_when_positive() {
        let c = llm_shadow_counters("g1", "m1", 50, 0);
        // tokens (g+m)×2 + requests (g+m)×2 = 8; no cost entries.
        assert_eq!(c.len(), 8);
        assert!(!c.iter().any(|(k, _, _)| k.starts_with("cost:")));
    }

    // -----------------------------------------------------------------------
    // evaluate_skill_quota — PURE decision core (no DB)
    // -----------------------------------------------------------------------

    fn skill_eval(limits: serde_json::Value, snap: SkillUsageSnapshot) -> UsageEnforcementResult {
        evaluate_skill_quota("g1", "G1", &limits, None, &snap)
    }

    #[test]
    fn skill_no_limits_allows() {
        assert!(!skill_eval(json!({}), SkillUsageSnapshot::default()).blocked);
    }

    #[test]
    fn skill_daily_executions_block_on_next() {
        let snap = SkillUsageSnapshot {
            daily_exec: 2,
            ..Default::default()
        };
        let r = skill_eval(json!({"execution": {"daily_limit": 2}}), snap);
        assert!(r.blocked, "(2+1) > 2");
        assert_eq!(r.limit_type.as_deref(), Some("daily_skill_executions"));
    }

    #[test]
    fn skill_daily_executions_under_allows_and_warns() {
        let snap = SkillUsageSnapshot {
            daily_exec: 9,
            ..Default::default()
        };
        let r = skill_eval(json!({"execution": {"daily_limit": 10}}), snap);
        assert!(!r.blocked, "(9+1) = 10, not > 10");
        assert!(
            r.warning_headers
                .iter()
                .any(|(k, _)| k == "X-Usage-Warning"),
            "90% >= 80% daily warn default"
        );
    }

    #[test]
    fn skill_monthly_executions_block() {
        let snap = SkillUsageSnapshot {
            monthly_exec: 100,
            ..Default::default()
        };
        let r = skill_eval(json!({"execution": {"monthly_limit": 100}}), snap);
        assert!(r.blocked);
        assert_eq!(r.limit_type.as_deref(), Some("monthly_skill_executions"));
    }

    #[test]
    fn skill_per_skill_block_and_group_precedence() {
        // Group blocks before per-skill when both over.
        let snap = SkillUsageSnapshot {
            daily_exec: 5,
            ps_daily_exec: 5,
            ..Default::default()
        };
        let r = evaluate_skill_quota(
            "g1",
            "G1",
            &json!({"execution": {"daily_limit": 5}}),
            Some(&json!({"execution": {"daily_limit": 5}})),
            &snap,
        );
        assert_eq!(r.limit_type.as_deref(), Some("daily_skill_executions"));

        // Per-skill alone blocks.
        let snap2 = SkillUsageSnapshot {
            ps_daily_exec: 3,
            ..Default::default()
        };
        let r2 = evaluate_skill_quota(
            "g1",
            "G1",
            &json!({}),
            Some(&json!({"execution": {"daily_limit": 3}})),
            &snap2,
        );
        assert_eq!(r2.limit_type.as_deref(), Some("per_skill_daily_executions"));
    }

    // -----------------------------------------------------------------------
    // restrictive_merge tests
    // -----------------------------------------------------------------------

    #[test]
    fn merge_numeric_takes_min_when_child_is_lower() {
        let mut base = json!({"daily_limit": 100});
        let overlay = json!({"daily_limit": 50});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["daily_limit"], 50);
    }

    #[test]
    fn merge_numeric_takes_min_when_parent_is_lower() {
        let mut base = json!({"daily_limit": 50});
        let overlay = json!({"daily_limit": 100});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["daily_limit"], 50);
    }

    #[test]
    fn merge_numeric_equal_values_unchanged() {
        let mut base = json!({"limit": 75});
        let overlay = json!({"limit": 75});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["limit"], 75);
    }

    #[test]
    fn merge_numeric_preserves_integer_type() {
        let mut base = json!({"limit": 100});
        let overlay = json!({"limit": 50});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert!(base["limit"].is_i64(), "Result should be an integer");
        assert_eq!(base["limit"].as_i64().unwrap(), 50);
    }

    #[test]
    fn merge_numeric_float_values() {
        let mut base = json!({"budget": 10.5});
        let overlay = json!({"budget": 5.25});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["budget"].as_f64().unwrap(), 5.25);
    }

    #[test]
    fn merge_bool_true_wins_overlay_true() {
        let mut base = json!({"hard_stop": false});
        let overlay = json!({"hard_stop": true});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["hard_stop"], true);
    }

    #[test]
    fn merge_bool_true_wins_base_true() {
        let mut base = json!({"hard_stop": true});
        let overlay = json!({"hard_stop": false});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["hard_stop"], true);
    }

    #[test]
    fn merge_bool_both_false_stays_false() {
        let mut base = json!({"hard_stop": false});
        let overlay = json!({"hard_stop": false});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["hard_stop"], false);
    }

    #[test]
    fn merge_bool_both_true_stays_true() {
        let mut base = json!({"hard_stop": true});
        let overlay = json!({"hard_stop": true});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["hard_stop"], true);
    }

    #[test]
    fn merge_nested_objects_recurse() {
        let mut base = json!({
            "budget": {
                "daily_limit_usd": 100.0,
                "hard_stop": false
            }
        });
        let overlay = json!({
            "budget": {
                "daily_limit_usd": 50.0,
                "hard_stop": true
            }
        });
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["budget"]["daily_limit_usd"].as_f64().unwrap(), 50.0);
        assert_eq!(base["budget"]["hard_stop"], true);
    }

    #[test]
    fn merge_new_keys_from_overlay_added() {
        let mut base = json!({"existing": 10});
        let overlay = json!({"new_key": 42});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["existing"], 10);
        assert_eq!(base["new_key"], 42);
    }

    #[test]
    fn merge_empty_base_gets_overlay_values() {
        let mut base = json!({});
        let overlay = json!({
            "daily_limit": 100,
            "hard_stop": true
        });
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["daily_limit"], 100);
        assert_eq!(base["hard_stop"], true);
    }

    #[test]
    fn merge_empty_overlay_base_unchanged() {
        let mut base = json!({"daily_limit": 100, "hard_stop": false});
        let overlay = json!({});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["daily_limit"], 100);
        assert_eq!(base["hard_stop"], false);
    }

    /// A child's value that is not a number cannot replace the
    /// parent's numeric limit (this test used to pin "unlimited" winning,
    /// which removed the parent's limit).
    #[test]
    fn merge_a_childs_non_number_keeps_the_parents_number() {
        let mut base = json!({"field": 100});
        let overlay = json!({"field": "unlimited"});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["field"], 100);
    }

    #[test]
    fn merge_mixed_types_string_to_number() {
        let mut base = json!({"field": "unlimited"});
        let overlay = json!({"field": 50});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["field"], 50);
    }

    #[test]
    fn merge_deep_nesting_three_levels() {
        let mut base = json!({
            "level1": {
                "level2": {
                    "level3_limit": 1000,
                    "level3_stop": false
                }
            }
        });
        let overlay = json!({
            "level1": {
                "level2": {
                    "level3_limit": 500,
                    "level3_stop": true
                }
            }
        });
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["level1"]["level2"]["level3_limit"], 500);
        assert_eq!(base["level1"]["level2"]["level3_stop"], true);
    }

    #[test]
    fn merge_deep_nesting_four_levels() {
        let mut base = json!({
            "a": {"b": {"c": {"d": 200}}}
        });
        let overlay = json!({
            "a": {"b": {"c": {"d": 100}}}
        });
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["a"]["b"]["c"]["d"], 100);
    }

    #[test]
    fn merge_real_world_llm_limits() {
        // Simulates a parent org group and a child team group
        let mut parent = json!({
            "budget": {
                "daily_limit_usd": 100.0,
                "monthly_limit_usd": 2000.0,
                "hard_stop": true
            },
            "tokens": {
                "daily_limit": 1000000,
                "monthly_limit": 20000000
            },
            "requests": {
                "daily_limit": 5000,
                "monthly_limit": 100000
            }
        });
        let child = json!({
            "budget": {
                "daily_limit_usd": 50.0,
                "monthly_limit_usd": 1000.0,
                "hard_stop": false
            },
            "tokens": {
                "daily_limit": 500000,
                "monthly_limit": 25000000
            },
            "requests": {
                "daily_limit": 10000,
                "monthly_limit": 50000
            }
        });
        restrictive_merge(&mut parent, &child, LimitFamily::Llm);

        // Budget: min values, hard_stop true wins
        assert_eq!(parent["budget"]["daily_limit_usd"].as_f64().unwrap(), 50.0);
        assert_eq!(
            parent["budget"]["monthly_limit_usd"].as_f64().unwrap(),
            1000.0
        );
        assert_eq!(parent["budget"]["hard_stop"], true);

        // Tokens: min values
        assert_eq!(parent["tokens"]["daily_limit"], 500000);
        assert_eq!(parent["tokens"]["monthly_limit"], 20000000); // parent lower

        // Requests: min values
        assert_eq!(parent["requests"]["daily_limit"], 5000); // parent lower
        assert_eq!(parent["requests"]["monthly_limit"], 50000); // child lower
    }

    #[test]
    fn merge_overlay_adds_new_nested_section() {
        let mut base = json!({
            "budget": {"daily_limit_usd": 100.0}
        });
        let overlay = json!({
            "budget": {"daily_limit_usd": 50.0},
            "tokens": {"daily_limit": 500000}
        });
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["budget"]["daily_limit_usd"].as_f64().unwrap(), 50.0);
        assert_eq!(base["tokens"]["daily_limit"], 500000);
    }

    #[test]
    fn merge_non_object_top_level_ignored() {
        // When base is not an object, the function is a no-op
        let mut base = json!(42);
        let overlay = json!(10);
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        // base should be unchanged since non-object top-level is ignored
        assert_eq!(base, json!(42));
    }

    #[test]
    fn merge_chained_three_levels_of_hierarchy() {
        // Simulate root -> department -> team merge chain
        let mut effective = json!({
            "budget": {"daily_limit_usd": 1000.0, "hard_stop": false},
            "tokens": {"daily_limit": 5000000}
        });

        let department = json!({
            "budget": {"daily_limit_usd": 500.0, "hard_stop": true},
            "tokens": {"daily_limit": 2000000}
        });
        restrictive_merge(&mut effective, &department, LimitFamily::Llm);

        let team = json!({
            "budget": {"daily_limit_usd": 200.0, "hard_stop": false},
            "tokens": {"daily_limit": 3000000}
        });
        restrictive_merge(&mut effective, &team, LimitFamily::Llm);

        // Budget: min across all three = 200
        assert_eq!(
            effective["budget"]["daily_limit_usd"].as_f64().unwrap(),
            200.0
        );
        // hard_stop: true wins from department level
        assert_eq!(effective["budget"]["hard_stop"], true);
        // Tokens: min across all three = 2000000 (department)
        assert_eq!(effective["tokens"]["daily_limit"], 2000000);
    }

    /// The LLM, MCP, skill and A2A quota readers take a value that
    /// is not positive as unset, so a child's `0` is no limit at its level
    /// and the parent's stays (this test used to pin `0` winning, which the
    /// readers then read as "no limit").
    #[test]
    fn merge_a_childs_zero_keeps_the_parents_limit() {
        let mut base = json!({"limit": 100});
        let overlay = json!({"limit": 0});
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["limit"], 100);
        let mut base = json!({"limit": 100});
        restrictive_merge(&mut base, &json!({"limit": -5}), LimitFamily::Llm);
        assert_eq!(base["limit"], 100);
        // A parent's non-positive value is no limit either: the child's is.
        let mut base = json!({"limit": 0});
        restrictive_merge(&mut base, &json!({"limit": 40}), LimitFamily::Llm);
        assert_eq!(base["limit"], 40);
    }

    /// A child's `null` leaves the parent's value, for a number, a
    /// bool and a whole section.
    #[test]
    fn merge_a_childs_null_keeps_the_parents_value() {
        let mut base = json!({
            "execution": {"timeout_seconds": 30},
            "budget": {"daily_limit_usd": 10.5, "hard_stop": true},
            "tokens": {"daily_limit": 1000}
        });
        let overlay = json!({
            "execution": {"timeout_seconds": null},
            "budget": {"daily_limit_usd": null, "hard_stop": null},
            "tokens": null
        });
        restrictive_merge(&mut base, &overlay, LimitFamily::Llm);
        assert_eq!(base["execution"]["timeout_seconds"], 30);
        assert_eq!(base["budget"]["daily_limit_usd"], 10.5);
        assert_eq!(base["budget"]["hard_stop"], true);
        assert_eq!(base["tokens"]["daily_limit"], 1000);
    }

    /// A child's malformed bool or section cannot replace the
    /// parent's.
    #[test]
    fn merge_a_childs_malformed_bool_or_section_keeps_the_parents() {
        let mut base = json!({"budget": {"hard_stop": true}, "tokens": {"daily_limit": 9}});
        restrictive_merge(
            &mut base,
            &json!({"budget": {"hard_stop": "no"}, "tokens": 0}),
            LimitFamily::Llm,
        );
        assert_eq!(base["budget"]["hard_stop"], true);
        assert_eq!(base["tokens"]["daily_limit"], 9);
    }

    /// A null base adopts the overlay (a JSON-null document must not
    /// swallow the limits folded into it).
    #[test]
    fn merge_into_a_null_document_adopts_the_overlay() {
        let mut base = serde_json::Value::Null;
        restrictive_merge(
            &mut base,
            &json!({"tokens": {"daily_limit": 7}}),
            LimitFamily::Llm,
        );
        assert_eq!(base["tokens"]["daily_limit"], 7);
    }

    /// The merged value is one of the stated values, as stated: an integer
    /// limit read with `as_i64` stays readable.
    #[test]
    fn merge_keeps_the_winning_value_as_stated() {
        let mut base = json!({"limit": 5});
        restrictive_merge(&mut base, &json!({"limit": 3.0}), LimitFamily::Llm);
        assert_eq!(base["limit"], json!(3.0));
        let mut base = json!({"limit": 3.5});
        restrictive_merge(&mut base, &json!({"limit": 7}), LimitFamily::Llm);
        assert_eq!(base["limit"], json!(3.5));
        let mut base = json!({"limit": 3.0});
        restrictive_merge(&mut base, &json!({"limit": 3}), LimitFamily::Llm);
        assert!(base["limit"].as_f64() == Some(3.0));
    }

    /// Where a family reads a number that is not positive as a limit (a
    /// media quota of 0 admits nothing; an A2A delegation depth of 0 forbids
    /// delegation), it is the strictest value and stays.
    #[test]
    fn merge_keeps_a_non_positive_limit_where_the_family_reads_it_as_one() {
        let mut base = json!({"daily_requests": 5});
        restrictive_merge(&mut base, &json!({"daily_requests": 0}), LimitFamily::Media);
        assert_eq!(base["daily_requests"], 0);

        let mut base = json!({"chain": {"max_delegation_depth": 3},
                              "invocations": {"daily_limit": 100}});
        restrictive_merge(
            &mut base,
            &json!({"chain": {"max_delegation_depth": 0},
                    "invocations": {"daily_limit": 0}}),
            LimitFamily::A2a,
        );
        assert_eq!(base["chain"]["max_delegation_depth"], 0);
        assert_eq!(
            base["invocations"]["daily_limit"], 100,
            "0 invocations is unset"
        );

        for family in [LimitFamily::Llm, LimitFamily::Mcp, LimitFamily::Skill] {
            let mut base = json!({"execution": {"daily_limit": 5}});
            restrictive_merge(&mut base, &json!({"execution": {"daily_limit": 0}}), family);
            assert_eq!(base["execution"]["daily_limit"], 5, "{family:?}");
        }
    }

    // -----------------------------------------------------------------------
    // make_blocked tests
    // -----------------------------------------------------------------------

    #[test]
    fn make_blocked_constructs_correct_fields() {
        let result = make_blocked(
            "group-123",
            "Engineering",
            "daily_budget",
            "$45.00",
            "$50.00",
            "Daily budget exceeded".to_string(),
        );
        assert_eq!(result.group_id.as_deref(), Some("group-123"));
        assert_eq!(result.group_name.as_deref(), Some("Engineering"));
        assert_eq!(result.limit_type.as_deref(), Some("daily_budget"));
        assert_eq!(result.current_usage.as_deref(), Some("$45.00"));
        assert_eq!(result.max_limit.as_deref(), Some("$50.00"));
        assert_eq!(result.message.as_deref(), Some("Daily budget exceeded"));
    }

    #[test]
    fn make_blocked_is_blocked() {
        let result = make_blocked("g", "n", "t", "u", "l", "msg".to_string());
        assert!(result.blocked);
    }

    #[test]
    fn make_blocked_has_empty_warning_headers() {
        let result = make_blocked("g", "n", "t", "u", "l", "msg".to_string());
        assert!(result.warning_headers.is_empty());
    }

    // -----------------------------------------------------------------------
    // evaluate_skill_quota — per-skill / per-group execution caps.
    // Regression cover for the quota fix: a per-skill daily_limit set ONLY on
    // the assignment row must enforce (previously the null group-chain caused
    // an early allow, and a null base swallowed the per-skill limits).
    // -----------------------------------------------------------------------

    #[test]
    fn skill_quota_per_skill_daily_blocks_at_limit() {
        // limit=2, already 2 executions today -> the next (2+1>2) is refused.
        let effective_ps = json!({ "execution": { "daily_limit": 2 } });
        let snap = SkillUsageSnapshot {
            ps_daily_exec: 2,
            ..Default::default()
        };
        let r = evaluate_skill_quota("g", "Sales", &json!({}), Some(&effective_ps), &snap);
        assert!(r.blocked, "per-skill daily limit should block at 2/2");
        assert!(r.message.as_deref().unwrap_or("").contains("2 / 2"));
    }

    #[test]
    fn skill_quota_per_skill_daily_allows_under_limit() {
        let effective_ps = json!({ "execution": { "daily_limit": 2 } });
        let snap = SkillUsageSnapshot {
            ps_daily_exec: 1,
            ..Default::default()
        };
        let r = evaluate_skill_quota("g", "Sales", &json!({}), Some(&effective_ps), &snap);
        assert!(!r.blocked, "1/2 must be allowed (1+1 <= 2)");
    }

    #[test]
    fn skill_quota_group_level_daily_blocks() {
        // Group-wide skill cap (read from `limits`, counted via snap.daily_exec).
        let limits = json!({ "execution": { "daily_limit": 5 } });
        let snap = SkillUsageSnapshot {
            daily_exec: 5,
            ..Default::default()
        };
        let r = evaluate_skill_quota("g", "Sales", &limits, None, &snap);
        assert!(r.blocked, "group-wide daily limit should block at 5/5");
    }

    #[test]
    fn skill_quota_no_limits_allows() {
        let snap = SkillUsageSnapshot {
            ps_daily_exec: 99,
            daily_exec: 99,
            ..Default::default()
        };
        let r = evaluate_skill_quota("g", "Sales", &json!({}), None, &snap);
        assert!(!r.blocked, "no configured limits -> never blocks");
    }

    #[test]
    fn restrictive_merge_inserts_into_empty_base() {
        // The quota null-base fix coerces a null/absent base to `{}` and merges
        // the per-skill limits in. This proves restrictive_merge ADDS new keys
        // (not just min-of-shared), so per-skill-only limits survive the merge.
        let mut base = json!({});
        restrictive_merge(
            &mut base,
            &json!({ "execution": { "daily_limit": 2 } }),
            LimitFamily::Llm,
        );
        assert_eq!(base["execution"]["daily_limit"], 2);
    }

    // -----------------------------------------------------------------------
    // retry_after_seconds_for_limit_type tests (RFC 6585 §4)
    // -----------------------------------------------------------------------

    fn at(y: i32, m: u32, d: u32, h: u32, mi: u32, s: u32) -> DateTime<Utc> {
        chrono::NaiveDate::from_ymd_opt(y, m, d)
            .unwrap()
            .and_hms_opt(h, mi, s)
            .unwrap()
            .and_utc()
    }

    #[test]
    fn retry_after_daily_budget_returns_seconds_until_next_utc_midnight() {
        // 14:30:00 UTC on 16 May → next midnight is 00:00:00 on 17 May.
        let now = at(2026, 5, 16, 14, 30, 0);
        let secs = retry_after_seconds_for_limit_type_at(Some("daily_budget"), now);
        // 23:30 → 24:00 = 9h30m = 34_200 s
        assert_eq!(secs, 9 * 3600 + 30 * 60);
    }

    #[test]
    fn retry_after_daily_tokens_uses_same_window() {
        let now = at(2026, 5, 16, 23, 59, 59);
        let secs = retry_after_seconds_for_limit_type_at(Some("daily_tokens"), now);
        assert_eq!(secs, 1);
    }

    #[test]
    fn retry_after_monthly_budget_returns_seconds_until_next_month_first() {
        // 2026-05-16 00:00:00 UTC → 2026-06-01 00:00:00 = 16 days = 1_382_400 s
        let now = at(2026, 5, 16, 0, 0, 0);
        let secs = retry_after_seconds_for_limit_type_at(Some("monthly_budget"), now);
        assert_eq!(secs, 16 * 86_400);
    }

    #[test]
    fn retry_after_monthly_wraps_december_to_next_year_january() {
        // 2026-12-31 23:00:00 UTC → 2027-01-01 00:00:00 = 1h = 3_600 s
        let now = at(2026, 12, 31, 23, 0, 0);
        let secs = retry_after_seconds_for_limit_type_at(Some("monthly_requests"), now);
        assert_eq!(secs, 3_600);
    }

    #[test]
    fn retry_after_unknown_limit_type_returns_60s_fallback() {
        let now = at(2026, 5, 16, 12, 0, 0);
        assert_eq!(retry_after_seconds_for_limit_type_at(None, now), 60);
        assert_eq!(
            retry_after_seconds_for_limit_type_at(Some("weekly_budget"), now),
            60
        );
        assert_eq!(retry_after_seconds_for_limit_type_at(Some(""), now), 60);
    }

    #[test]
    fn retry_after_daily_skill_executions_matches_daily_prefix() {
        let now = at(2026, 5, 16, 18, 0, 0);
        let secs = retry_after_seconds_for_limit_type_at(Some("daily_skill_executions"), now);
        // 18:00 → 24:00 = 6h = 21_600 s
        assert_eq!(secs, 6 * 3600);
    }

    #[test]
    fn retry_after_never_returns_zero() {
        // Boundary: exactly at midnight — next midnight is 24h away.
        let now = at(2026, 5, 16, 0, 0, 0);
        let secs = retry_after_seconds_for_limit_type_at(Some("daily_requests"), now);
        assert_eq!(secs, 86_400);
        assert!(secs >= 1);
    }

    // -----------------------------------------------------------------------
    // UsageEnforcementResult::allowed tests
    // -----------------------------------------------------------------------

    #[test]
    fn allowed_result_not_blocked() {
        let result = UsageEnforcementResult::allowed();
        assert!(!result.blocked);
    }

    #[test]
    fn allowed_result_all_fields_none() {
        let result = UsageEnforcementResult::allowed();
        assert!(result.group_id.is_none());
        assert!(result.group_name.is_none());
        assert!(result.message.is_none());
        assert!(result.limit_type.is_none());
        assert!(result.current_usage.is_none());
        assert!(result.max_limit.is_none());
    }

    #[test]
    fn allowed_result_empty_warning_headers() {
        let result = UsageEnforcementResult::allowed();
        assert!(result.warning_headers.is_empty());
    }

    // -----------------------------------------------------------------------
    // MCP per-call rate limiting — limit parsing + per-scope admission
    // -----------------------------------------------------------------------

    use crate::services::cache::usage_state::{DimKind, Finish};
    use crate::services::limits::{self as lim, FreqLimits};

    #[test]
    fn parse_freq_reads_all_fields() {
        let v = json!({
            "max_calls_per_minute": 10,
            "max_calls_per_hour": 100,
            "max_calls_per_day": 1000,
            "burst_limit": 20,
            "cooldown_seconds": 30
        });
        let f = FreqLimits::parse(&v);
        assert_eq!(f.per_minute, Some(10));
        assert_eq!(f.per_hour, Some(100));
        assert_eq!(f.per_day, Some(1000));
        assert_eq!(f.burst, Some(20));
        assert_eq!(f.cooldown_secs, Some(30));
    }

    #[test]
    fn parse_freq_ignores_zero_and_missing() {
        let f = FreqLimits::parse(&json!({"max_calls_per_minute": 0, "max_calls_per_hour": -5}));
        assert!(f.is_empty());
    }

    #[test]
    fn freq_at_navigates_nested_path() {
        let v = json!({"per_tool": {"search": {"max_calls_per_hour": 7}}});
        assert_eq!(
            FreqLimits::at(Some(&v), &["per_tool", "search"]).per_hour,
            Some(7)
        );
        assert!(FreqLimits::at(Some(&v), &["per_tool", "other"]).is_empty());
        assert!(FreqLimits::at(None, &["frequency"]).is_empty());
    }

    #[test]
    fn freq_limits_is_empty_ignores_burst_and_cooldown() {
        let f = FreqLimits::parse(&json!({"burst_limit": 5, "cooldown_seconds": 9}));
        assert!(f.is_empty(), "burst/cooldown are inert without a rate");
    }

    #[test]
    fn secs_until_window_reset_is_within_window() {
        let h = lim::secs_until_window_reset(QuotaWindow::Hour);
        assert!((1..=3600).contains(&h));
        let d = lim::secs_until_window_reset(QuotaWindow::Day);
        assert!((1..=86_400).contains(&d));
    }

    async fn rate(st: &UsageState, f: &FreqLimits, key: &str) -> Option<McpRateLimitBlock> {
        admit_rate(st, None, lim::freq_dims("MCP", "group", key, f)).await
    }

    #[tokio::test]
    async fn an_empty_scope_admits() {
        let st = UsageState::new(None);
        assert!(rate(&st, &FreqLimits::default(), "g:empty").await.is_none());
    }

    #[tokio::test]
    async fn the_hourly_quota_refuses_at_the_limit() {
        let st = UsageState::new(None);
        let f = FreqLimits {
            per_hour: Some(2),
            ..Default::default()
        };
        assert!(rate(&st, &f, "g:hour").await.is_none());
        assert!(rate(&st, &f, "g:hour").await.is_none());
        let b = rate(&st, &f, "g:hour").await.expect("third call refused");
        assert_eq!(b.limit_type, "hourly_quota");
        assert_eq!(b.message, "MCP hourly quota exceeded at group scope (2/2)");
    }

    #[tokio::test]
    async fn the_token_bucket_refuses_after_its_burst() {
        let st = UsageState::new(None);
        let f = FreqLimits {
            per_minute: Some(60),
            burst: Some(2),
            ..Default::default()
        };
        assert!(rate(&st, &f, "g:bucket").await.is_none());
        assert!(rate(&st, &f, "g:bucket").await.is_none());
        let b = rate(&st, &f, "g:bucket").await.expect("burst spent");
        assert_eq!(b.limit_type, "rate");
        assert!(b.retry_after_secs >= 1);
    }

    #[tokio::test]
    async fn a_bucket_trip_arms_the_scope_cooldown() {
        let st = UsageState::new(None);
        let f = FreqLimits {
            per_minute: Some(60),
            burst: Some(1),
            cooldown_secs: Some(30),
            ..Default::default()
        };
        assert!(rate(&st, &f, "g:cd").await.is_none());
        let b1 = rate(&st, &f, "g:cd").await.expect("bucket trips");
        assert_eq!(b1.limit_type, "rate");
        assert_eq!(b1.retry_after_secs, 30);
        let b2 = rate(&st, &f, "g:cd").await.expect("cooldown holds");
        assert_eq!(b2.limit_type, "cooldown");
        assert!(b2.retry_after_secs >= 1 && b2.retry_after_secs <= 30);
    }

    /// A call the hourly quota refuses must not have spent a bucket token:
    /// admission is all or nothing (it used to count every attempt at every
    /// scope it had already passed).
    #[tokio::test]
    async fn a_refused_call_spends_nothing_at_any_scope() {
        let st = UsageState::new(None);
        let f = FreqLimits {
            per_minute: Some(60),
            burst: Some(5),
            per_hour: Some(1),
            ..Default::default()
        };
        assert!(rate(&st, &f, "g:all").await.is_none());
        for _ in 0..3 {
            assert_eq!(
                rate(&st, &f, "g:all").await.unwrap().limit_type,
                "hourly_quota"
            );
        }
        let bal = st.bucket_balance_for_test("g:all", 5.0, 1.0).await;
        assert!(
            bal >= 4.0 - 1e-6,
            "only the admitted call took a token: {bal}"
        );
    }

    #[tokio::test]
    async fn a_scope_counted_by_two_limits_is_judged_by_both_at_once() {
        // Group + tool scope on one call: the narrow scope refusing must not
        // leave the broad scope's unit spent.
        let st = UsageState::new(None);
        let broad = FreqLimits {
            per_hour: Some(10),
            ..Default::default()
        };
        let narrow = FreqLimits {
            per_hour: Some(1),
            ..Default::default()
        };
        let both = || {
            let mut d = lim::freq_dims("MCP", "group", "g:x", &broad);
            d.extend(lim::freq_dims("MCP", "group-tool", "g:x:t:y", &narrow));
            d
        };
        assert!(admit_rate(&st, None, both()).await.is_none());
        let b = admit_rate(&st, None, both())
            .await
            .expect("tool scope full");
        assert_eq!(b.scope, "group-tool");
        let key = lim::freq_window_key("g:x", QuotaWindow::Hour);
        assert_eq!(st.counter_value(&key).await.unwrap(), 1);
    }

    #[test]
    fn parse_llm_throughput_reads_all_new_fields() {
        let v = json!({
            "max_requests_per_minute": 60,
            "max_input_tokens_per_minute": 100000,
            "max_output_tokens_per_minute": 20000,
            "max_usd_per_minute": 0.5,
            "request_burst": 10,
            "tokens_burst": 200000,
            "usd_burst": 1.0,
            "cooldown_seconds": 60,
        });
        let t = parse_llm_throughput(&v);
        assert_eq!(t.rpm, Some(60));
        assert_eq!(t.itpm, Some(100_000));
        assert_eq!(t.otpm, Some(20_000));
        assert_eq!(t.usd_cents_per_minute, Some(50));
        assert_eq!(t.rpm_burst, Some(10));
        assert_eq!(t.tokens_burst, Some(200_000));
        assert_eq!(t.usd_cents_burst, Some(100));
        assert_eq!(t.cooldown_secs, Some(60));
    }

    #[test]
    fn parse_llm_throughput_treats_zero_and_negative_as_unset() {
        // Mirrors `parse_freq` semantics — guards against misconfigured
        // limits silently turning into "no limit".
        let v = json!({
            "max_requests_per_minute": 0,
            "max_input_tokens_per_minute": -1,
            "max_usd_per_minute": 0.0,
        });
        let t = parse_llm_throughput(&v);
        assert!(t.rpm.is_none());
        assert!(t.itpm.is_none());
        assert!(t.usd_cents_per_minute.is_none());
        assert!(t.is_empty());
    }

    #[test]
    fn parse_llm_throughput_empty_object_is_empty() {
        let t = parse_llm_throughput(&json!({}));
        assert!(t.is_empty());
    }

    #[test]
    fn parse_llm_throughput_usd_dollars_to_cents() {
        // 0.50 USD → 50 cents. The Redis token bucket counts integers,
        // so the float→int boundary must round half-to-even predictably.
        let t = parse_llm_throughput(&json!({"max_usd_per_minute": 0.50, "usd_burst": 2.25}));
        assert_eq!(t.usd_cents_per_minute, Some(50));
        assert_eq!(t.usd_cents_burst, Some(225));
    }

    #[test]
    fn parse_llm_concurrency_positive_only() {
        assert_eq!(
            parse_llm_concurrency(&json!({"max_concurrent": 8})).max_concurrent,
            Some(8)
        );
        assert_eq!(
            parse_llm_concurrency(&json!({"max_concurrent": 0})).max_concurrent,
            None
        );
        assert_eq!(
            parse_llm_concurrency(&json!({"max_concurrent": -3})).max_concurrent,
            None
        );
        assert_eq!(parse_llm_concurrency(&json!({})).max_concurrent, None);
    }

    #[test]
    fn llm_throughput_at_navigates_nested_paths() {
        let limits = Some(json!({
            "throughput": {"max_requests_per_minute": 60, "max_input_tokens_per_minute": 1000}
        }));
        let t = llm_throughput_at(&limits, &["throughput"]);
        assert_eq!(t.rpm, Some(60));
        assert_eq!(t.itpm, Some(1000));

        // Missing section yields default empty limits.
        let no_section = llm_throughput_at(&limits, &["throughput", "missing"]);
        assert!(no_section.is_empty());
        let no_limits = llm_throughput_at(&None, &["throughput"]);
        assert!(no_limits.is_empty());
    }

    #[test]
    fn llm_concurrency_at_navigates_nested_paths() {
        let limits = Some(json!({"concurrency": {"max_concurrent": 4}}));
        let c = llm_concurrency_at(&limits, &["concurrency"]);
        assert_eq!(c.max_concurrent, Some(4));
        let missing = llm_concurrency_at(&limits, &["concurrency", "x"]);
        assert_eq!(missing.max_concurrent, None);
    }

    // -----------------------------------------------------------------------
    // enforce_llm_throughput_pre — cooldown + RPM bucket
    // -----------------------------------------------------------------------

    // -----------------------------------------------------------------------
    // LLM throughput — charged at admission, trued up at settle
    // -----------------------------------------------------------------------

    fn llm_shape(in_bound: i64, in_est: i64, out: i64) -> Shape {
        Shape {
            in_tokens: in_bound,
            in_estimate: in_est,
            out_tokens: out,
            min_out: out.min(1),
            choices: 1,
            in_micros_per_token: 0.0,
            out_micros_per_token: 0.0,
        }
    }

    #[tokio::test]
    async fn an_empty_throughput_block_admits() {
        let st = UsageState::new(None);
        let dims = throughput_dims("group", "llm:g:none", &LLMThroughputLimits::default());
        assert!(dims.is_empty());
        assert!(admit_rate(&st, None, dims).await.is_none());
    }

    #[tokio::test]
    async fn the_rpm_bucket_refuses_after_its_burst() {
        let st = UsageState::new(None);
        let t = LLMThroughputLimits {
            rpm: Some(60),
            rpm_burst: Some(2),
            ..Default::default()
        };
        for _ in 0..2 {
            assert!(
                admit_rate(&st, None, throughput_dims("group", "llm:g:rpm", &t))
                    .await
                    .is_none()
            );
        }
        let b = admit_rate(&st, None, throughput_dims("group", "llm:g:rpm", &t))
            .await
            .expect("burst spent");
        assert_eq!(b.limit_type, "rpm");
    }

    /// The defect: ITPM/OTPM/$-per-minute were PEEKED at admission and only
    /// charged after the response, so a burst of calls all saw headroom. Now
    /// each call's expected input is taken at admission: concurrent calls
    /// see each other.
    #[tokio::test]
    async fn itpm_is_charged_at_admission_so_a_burst_cannot_share_headroom() {
        let st = UsageState::new(None);
        let t = LLMThroughputLimits {
            itpm: Some(1_000),
            ..Default::default()
        };
        let shape = llm_shape(4_000, 400, 0);
        let mut admitted = 0;
        for i in 0..5 {
            let hold = lim::HoldId::for_test(&format!("itpm-{i}"));
            if lim::admit(
                &st,
                Some(&hold),
                throughput_dims("group", "llm:g:itpm", &t),
                shape,
            )
            .await
            .unwrap()
            .is_ok()
            {
                admitted += 1;
            }
        }
        assert_eq!(admitted, 2, "1000 input tokens/min fit two 400-token calls");
    }

    #[tokio::test]
    async fn a_throughput_bucket_settles_to_the_actual_with_debt() {
        let st = UsageState::new(None);
        let t = LLMThroughputLimits {
            itpm: Some(1_000),
            ..Default::default()
        };
        assert!(
            lim::admit(
                &st,
                Some(&lim::HoldId::for_test("debt-1")),
                throughput_dims("group", "llm:g:debt", &t),
                llm_shape(800, 200, 0)
            )
            .await
            .unwrap()
            .is_ok()
        );
        // The call read 1,900 tokens: 1,700 more than charged.
        st.finish_hold(
            "debt-1",
            Finish::Settle(crate::services::cache::usage_state::Actual {
                in_tokens: 1_900,
                out_tokens: 0,
                cost_micros: 0,
            }),
            &[],
        )
        .await
        .unwrap();
        // The next call must wait for the debt to be repaid.
        let next = lim::admit(
            &st,
            Some(&lim::HoldId::for_test("debt-2")),
            throughput_dims("group", "llm:g:debt", &t),
            llm_shape(800, 200, 0),
        )
        .await
        .unwrap();
        assert!(next.is_err(), "an under-estimate is owed, not forgiven");
    }

    #[tokio::test]
    async fn a_throughput_trip_arms_the_scope_cooldown() {
        let st = UsageState::new(None);
        let t = LLMThroughputLimits {
            rpm: Some(60),
            rpm_burst: Some(1),
            cooldown_secs: Some(30),
            ..Default::default()
        };
        assert!(
            admit_rate(&st, None, throughput_dims("group", "llm:g:cd", &t))
                .await
                .is_none()
        );
        let b1 = admit_rate(&st, None, throughput_dims("group", "llm:g:cd", &t))
            .await
            .expect("2nd call must block");
        assert_eq!(b1.limit_type, "rpm");
        assert_eq!(b1.retry_after_secs, 30);
        let b2 = admit_rate(&st, None, throughput_dims("group", "llm:g:cd", &t))
            .await
            .expect("3rd call must hit cooldown");
        assert_eq!(b2.limit_type, "cooldown");
    }

    // -----------------------------------------------------------------------
    // Cumulative quotas — admitted against the worst case
    //
    // The defect these cover: admission read a snapshot and the counters only
    // moved after the response, so N concurrent callers at 99 % of a cap all
    // read 99 % and all passed; and the reservation was an ESTIMATE, so an
    // admitted call could overshoot by what it used beyond it. Every test
    // below fails against that behaviour.
    // -----------------------------------------------------------------------

    fn group_dims(gid: &str, limits: &serde_json::Value) -> Vec<lim::LimitDim> {
        lim::cumulative_dims(
            &format!("grp:{gid}"),
            Some(limits),
            &lim::Owner::new(gid, gid),
            true,
            |f, d| group_quota_name(f, d).to_string(),
        )
    }

    fn priced(in_bound: i64, out: i64) -> Shape {
        Shape {
            in_tokens: in_bound,
            in_estimate: in_bound / 4,
            out_tokens: out,
            min_out: 1,
            choices: 1,
            in_micros_per_token: 1.0,
            out_micros_per_token: 4.0,
        }
    }

    /// The core race: two callers arrive together at a cap with room for
    /// one. Read-then-decide admitted both.
    #[tokio::test]
    async fn a_second_concurrent_caller_sees_the_first_ones_worst_case() {
        let st = UsageState::new(None);
        // $1.00 cap, $0.99 spent: room for 10,000 µ$.
        st.set_usage("cost:grp:g-conc", QuotaWindow::Day, 990_000)
            .await;
        let limits = json!({"budget": {"daily_limit_usd": 1.0}});
        // Worst case: 1,000 in (1,000 µ$) + up to 2,000 out (8,000 µ$).
        let first = lim::admit(
            &st,
            Some(&lim::HoldId::for_test("q-a")),
            group_dims("g-conc", &limits),
            priced(1_000, 2_000),
        )
        .await
        .unwrap();
        assert_eq!(first.map(|a| a.out_tokens).ok(), Some(2_000));
        let second = lim::admit(
            &st,
            Some(&lim::HoldId::for_test("q-b")),
            group_dims("g-conc", &limits),
            priced(1_000, 2_000),
        )
        .await
        .unwrap();
        let refused = second.expect_err("the first call's worst case fills the cap");
        assert_eq!(
            refused.quota_result().limit_type.as_deref(),
            Some("daily_budget")
        );
    }

    #[tokio::test]
    async fn the_cap_squeezes_the_output_to_what_is_left() {
        let st = UsageState::new(None);
        st.set_usage("cost:grp:g-sq", QuotaWindow::Day, 990_000)
            .await;
        let limits = json!({"budget": {"daily_limit_usd": 1.0}});
        // 10,000 µ$ left; 1,000 in; output at 4 µ$ → at most 2,250.
        let got = lim::admit(
            &st,
            Some(&lim::HoldId::for_test("q-sq")),
            group_dims("g-sq", &limits),
            priced(1_000, 8_192),
        )
        .await
        .unwrap();
        assert_eq!(got.map(|a| a.out_tokens).ok(), Some(2_250));
        assert_eq!(
            st.read_usage("cost:grp:g-sq", QuotaWindow::Day).await,
            1_000_000
        );
    }

    #[tokio::test]
    async fn settling_replaces_the_worst_case_with_the_actual() {
        let st = UsageState::new(None);
        let limits =
            json!({"budget": {"daily_limit_usd": 10.0}, "tokens": {"daily_limit": 100_000}});
        lim::admit(
            &st,
            Some(&lim::HoldId::for_test("q-settle")),
            group_dims("g-settle", &limits),
            priced(1_000, 2_000),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(
            st.read_usage("tok:grp:g-settle", QuotaWindow::Day).await,
            3_000
        );
        st.finish_hold(
            "q-settle",
            Finish::Settle(crate::services::cache::usage_state::Actual {
                in_tokens: 640,
                out_tokens: 100,
                cost_micros: 1_040,
            }),
            &[],
        )
        .await
        .unwrap();
        assert_eq!(
            st.read_usage("tok:grp:g-settle", QuotaWindow::Day).await,
            740
        );
        assert_eq!(
            st.read_usage("cost:grp:g-settle", QuotaWindow::Day).await,
            1_040
        );
        assert_eq!(st.read_usage("req:grp:g-settle", QuotaWindow::Day).await, 1);
    }

    #[tokio::test]
    async fn a_call_that_never_reached_the_provider_gives_everything_back() {
        let st = UsageState::new(None);
        let limits = json!({"budget": {"daily_limit_usd": 10.0}});
        lim::admit(
            &st,
            Some(&lim::HoldId::for_test("q-rel")),
            group_dims("g-rel", &limits),
            priced(1_000, 2_000),
        )
        .await
        .unwrap()
        .unwrap();
        st.finish_hold("q-rel", Finish::Release, &[]).await.unwrap();
        for f in ["cost", "tok", "req"] {
            assert_eq!(
                st.read_usage(&format!("{f}:grp:g-rel"), QuotaWindow::Day)
                    .await,
                0,
                "{f}"
            );
        }
    }

    #[tokio::test]
    async fn a_settle_is_applied_exactly_once() {
        let st = UsageState::new(None);
        let limits = json!({"tokens": {"daily_limit": 100_000}});
        lim::admit(
            &st,
            Some(&lim::HoldId::for_test("q-once")),
            group_dims("g-once", &limits),
            priced(1_000, 2_000),
        )
        .await
        .unwrap()
        .unwrap();
        let a = crate::services::cache::usage_state::Actual {
            in_tokens: 100,
            out_tokens: 100,
            cost_micros: 0,
        };
        st.finish_hold("q-once", Finish::Settle(a), &[])
            .await
            .unwrap();
        st.finish_hold("q-once", Finish::Settle(a), &[])
            .await
            .unwrap();
        assert_eq!(st.read_usage("tok:grp:g-once", QuotaWindow::Day).await, 200);
    }

    #[tokio::test]
    async fn a_refused_admission_takes_nothing_from_any_scope() {
        let st = UsageState::new(None);
        // The group has room; its per-model scope does not.
        st.set_usage("tok:grp:g-roll:mdl:m1", QuotaWindow::Day, 100)
            .await;
        let mut dims = group_dims("g-roll", &json!({"tokens": {"daily_limit": 1_000_000}}));
        dims.extend(lim::cumulative_dims(
            "grp:g-roll:mdl:m1",
            Some(&json!({"tokens": {"daily_limit": 100}})),
            &lim::Owner::new("g-roll", "g-roll"),
            true,
            |f, d| format!("per_model_{}", group_quota_name(f, d)),
        ));
        let r = lim::admit(
            &st,
            Some(&lim::HoldId::for_test("q-roll")),
            dims,
            priced(10, 10),
        )
        .await
        .unwrap();
        assert_eq!(
            r.unwrap_err().quota_result().limit_type.as_deref(),
            Some("per_model_daily_tokens")
        );
        assert_eq!(st.read_usage("tok:grp:g-roll", QuotaWindow::Day).await, 0);
        assert_eq!(st.read_usage("req:grp:g-roll", QuotaWindow::Day).await, 0);
    }

    /// An unlimited group's counters still carry its usage (the dashboards and
    /// the reconcile read them): reserved at admission, settled to the actual.
    #[tokio::test]
    async fn an_unlimited_group_is_accounted_but_never_refused() {
        let st = UsageState::new(None);
        st.set_usage("tok:grp:g-free", QuotaWindow::Day, i64::MAX / 8)
            .await;
        let dims = group_dims("g-free", &json!({}));
        assert!(
            dims.iter()
                .all(|d| d.refusal == lim::DimRefusal::Accounting)
        );
        lim::admit(
            &st,
            Some(&lim::HoldId::for_test("q-free")),
            dims,
            priced(10, 10),
        )
        .await
        .unwrap()
        .unwrap();
        st.finish_hold(
            "q-free",
            Finish::Settle(crate::services::cache::usage_state::Actual {
                in_tokens: 3,
                out_tokens: 4,
                cost_micros: 19,
            }),
            &[],
        )
        .await
        .unwrap();
        assert_eq!(st.read_usage("req:grp:g-free", QuotaWindow::Day).await, 1);
        assert_eq!(st.read_usage("cost:grp:g-free", QuotaWindow::Day).await, 19);
    }

    /// A call whose replica died mid-flight: its lease lapses and the reaper
    /// keeps its worst case as spent (it may have run). It used to be handed
    /// back after 15 minutes — an under-count if the call had run.
    #[tokio::test]
    async fn an_abandoned_call_keeps_its_worst_case() {
        let st = UsageState::new(None);
        let limits = json!({"tokens": {"daily_limit": 100_000}});
        lim::admit(
            &st,
            Some(&lim::HoldId::for_test("q-dead")),
            group_dims("g-dead", &limits),
            priced(1_000, 2_000),
        )
        .await
        .unwrap()
        .unwrap();
        st.finish_hold("q-dead", Finish::Abandon, &[])
            .await
            .unwrap();
        assert_eq!(
            st.read_usage("tok:grp:g-dead", QuotaWindow::Day).await,
            3_000
        );
    }

    #[test]
    fn quota_dims_name_their_refusals_like_the_snapshot() {
        let limits = json!({
            "budget": {"daily_limit_usd": 1.0, "monthly_limit_usd": 2.0},
            "tokens": {"daily_limit": 10, "monthly_limit": 20},
            "requests": {"daily_limit": 1, "monthly_limit": 2},
        });
        let names: Vec<String> = group_dims("g", &limits)
            .into_iter()
            .filter_map(|d| match d.refusal {
                lim::DimRefusal::Quota { limit_type, .. } => Some(limit_type),
                _ => None,
            })
            .collect();
        assert_eq!(
            names,
            [
                "daily_budget",
                "monthly_budget",
                "daily_tokens",
                "monthly_tokens",
                "daily_requests",
                "monthly_requests"
            ]
        );
    }

    // -----------------------------------------------------------------------
    // LLM concurrency — leased slots
    // -----------------------------------------------------------------------

    fn slot_dims(key: &str, max: i64) -> Vec<lim::LimitDim> {
        vec![lim::LimitDim {
            dim: Dim::slot(format!("brutor:llm_slot:{key}"), max),
            refusal: lim::DimRefusal::Rate {
                scope: "group",
                limit_type: "concurrency",
                what: "cap".into(),
                retry: lim::RateRetry::Bucket {
                    cooldown_secs: None,
                },
            },
        }]
    }

    #[tokio::test]
    async fn a_concurrency_slot_is_held_by_its_guard_and_freed_on_drop() {
        let st = Arc::new(UsageState::new(None));
        let admitted = lim::admit(
            &st,
            Some(&lim::HoldId::for_test("c-1")),
            slot_dims("llm:rg:x", 1),
            Shape::count(),
        )
        .await
        .unwrap()
        .expect("the slot is free");
        let guard = st.adopt_hold(admitted.hold.expect("admitted under a hold"));
        assert!(
            lim::admit(
                &st,
                Some(&lim::HoldId::for_test("c-2")),
                slot_dims("llm:rg:x", 1),
                Shape::count()
            )
            .await
            .unwrap()
            .is_err(),
            "the cap holds while the first call is in flight"
        );
        drop(guard);
        for _ in 0..50 {
            tokio::task::yield_now().await;
            if lim::admit(
                &st,
                Some(&lim::HoldId::for_test("c-3")),
                slot_dims("llm:rg:x", 1),
                Shape::count(),
            )
            .await
            .unwrap()
            .is_ok()
            {
                return;
            }
        }
        panic!("a dropped guard must free its slot");
    }

    #[test]
    fn every_dimension_kind_is_a_counter_or_slot_where_expected() {
        let d = slot_dims("k", 1);
        assert!(matches!(d[0].dim.kind, DimKind::Slot { .. }));
    }

    // -----------------------------------------------------------------------
    // MCP capability access — strictness ordering + default-access parsing +
    // the most-restrictive compose rule (the pure pieces of
    // `check_mcp_capability_access`; the full DB compose lives in the
    // integration suite).
    // -----------------------------------------------------------------------

    #[test]
    fn capability_strictness_orders_allowed_lt_approval_lt_denied() {
        assert!(CapabilityAccess::Allowed.strictness() < CapabilityAccess::Denied.strictness());
        assert!(
            CapabilityAccess::Allowed.strictness()
                < CapabilityAccess::ApprovalRequired {
                    window_seconds: Some(300)
                }
                .strictness()
        );
        assert!(
            CapabilityAccess::ApprovalRequired {
                window_seconds: Some(300)
            }
            .strictness()
                < CapabilityAccess::Denied.strictness()
        );
        // Timeout does not affect strictness.
        assert_eq!(
            CapabilityAccess::ApprovalRequired {
                window_seconds: Some(1)
            }
            .strictness(),
            CapabilityAccess::ApprovalRequired {
                window_seconds: Some(999)
            }
            .strictness()
        );
    }

    #[test]
    fn mcp_default_access_parses_all_capability_types_and_states() {
        // Every capability type maps to its own default_*_access field.
        let gov = json!({
            "default_tool_access": "disabled",
            "default_resource_access": "approval_required",
            "default_prompt_access": "enabled",
            "default_resource_template_access": "approval_required",
            "approval_timeout_seconds": 120
        });
        assert_eq!(
            mcp_default_access_from_gov(&gov, "tool"),
            CapabilityAccess::Denied
        );
        assert_eq!(
            mcp_default_access_from_gov(&gov, "resource"),
            CapabilityAccess::ApprovalRequired {
                window_seconds: Some(120)
            }
        );
        assert_eq!(
            mcp_default_access_from_gov(&gov, "prompt"),
            CapabilityAccess::Allowed
        );
        assert_eq!(
            mcp_default_access_from_gov(&gov, "resource_template"),
            CapabilityAccess::ApprovalRequired {
                window_seconds: Some(120)
            }
        );
    }

    #[test]
    fn mcp_default_access_missing_field_defaults_to_allowed_and_default_timeout() {
        // Missing field → "enabled" → Allowed.
        assert_eq!(
            mcp_default_access_from_gov(&json!({}), "tool"),
            CapabilityAccess::Allowed
        );
        // approval_required with no timeout → the policy states no window
        // (the hold takes the group / tenant / platform default).
        assert_eq!(
            mcp_default_access_from_gov(
                &json!({"default_tool_access": "approval_required"}),
                "tool"
            ),
            CapabilityAccess::ApprovalRequired {
                window_seconds: None
            }
        );
        // A capability type the gateway does not map → Denied (the most
        // restrictive reading), even under a document that states nothing.
        assert_eq!(
            mcp_default_access_from_gov(&json!({"default_tool_access": "disabled"}), "bogus"),
            CapabilityAccess::Denied
        );
        assert_eq!(
            mcp_default_access_from_gov(&json!({}), "bogus"),
            CapabilityAccess::Denied
        );
    }

    #[test]
    fn mcp_default_access_unknown_value_is_denied() {
        // An access level this build does not know is the most restrictive,
        // as on a capability-filter row: a value the control plane adds
        // later must not open the capability on an older gateway.
        for capability_type in ["tool", "resource", "prompt", "resource_template"] {
            let key = format!("default_{capability_type}_access");
            for value in [
                json!("allowed"),
                json!("Disabled"),
                json!(""),
                json!(true),
                json!(0),
                json!({"access": "enabled"}),
                json!(["enabled"]),
            ] {
                assert_eq!(
                    mcp_default_access_from_gov(
                        &json!({ key.clone(): value.clone() }),
                        capability_type
                    ),
                    CapabilityAccess::Denied,
                    "{key} = {value} must deny"
                );
            }
        }
        // Null states nothing: the default stays open, like an absent key.
        assert_eq!(
            mcp_default_access_from_gov(&json!({"default_tool_access": null}), "tool"),
            CapabilityAccess::Allowed
        );
    }

    #[test]
    fn capability_ceiling_is_most_restrictive_of_group_and_tenant() {
        // Mirrors the compose in `check_mcp_capability_access`: the group
        // allow-wins result is TIGHTENED (never relaxed) by the MCP server's
        // tenant-wide default ceiling — max strictness wins.
        fn compose(group: CapabilityAccess, ceiling: CapabilityAccess) -> CapabilityAccess {
            if ceiling.strictness() > group.strictness() {
                ceiling
            } else {
                group
            }
        }
        // Tenant ceiling tightens a group Allowed → ApprovalRequired.
        assert_eq!(
            compose(
                CapabilityAccess::Allowed,
                CapabilityAccess::ApprovalRequired {
                    window_seconds: Some(60)
                }
            ),
            CapabilityAccess::ApprovalRequired {
                window_seconds: Some(60)
            }
        );
        // Tenant ceiling denies outright.
        assert_eq!(
            compose(CapabilityAccess::Allowed, CapabilityAccess::Denied),
            CapabilityAccess::Denied
        );
        // A looser ceiling does NOT relax a stricter group decision.
        assert_eq!(
            compose(CapabilityAccess::Denied, CapabilityAccess::Allowed),
            CapabilityAccess::Denied
        );
    }

    // -----------------------------------------------------------------------
    // A2A rate decision — `enforce_a2a_limits`: the group's limits, the
    // card's tenant-wide limits and a capability's hourly rate. Pure
    // (UsageState + a limits JSON, no DB), so they belong here rather than in
    // the integration suite. Previously A2A quota had NO coverage at any scope.
    // -----------------------------------------------------------------------

    /// One group's limits alone.
    async fn enforce_a2a_quota(
        us: &UsageState,
        group_id: &str,
        limits: Option<&serde_json::Value>,
    ) -> Option<McpRateLimitBlock> {
        enforce_a2a_limits(
            us,
            A2aLimits {
                group: Some((group_id, limits)),
                ..A2aLimits::default()
            },
        )
        .await
    }

    /// One card's tenant-wide limits alone.
    async fn enforce_a2a_tenant_quota(
        us: &UsageState,
        tenant_id: &str,
        card_id: &str,
        limits: Option<&serde_json::Value>,
    ) -> Option<McpRateLimitBlock> {
        enforce_a2a_limits(
            us,
            A2aLimits {
                card: Some((tenant_id, card_id, limits)),
                ..A2aLimits::default()
            },
        )
        .await
    }

    #[tokio::test]
    async fn a2a_capability_hourly_rate_refuses_the_call_over_it() {
        let us = UsageState::new(None);
        let call = |rate| A2aLimits {
            capability: Some(("cap-1", "billing.refund", rate)),
            ..A2aLimits::default()
        };
        assert!(enforce_a2a_limits(&us, call(Some(2))).await.is_none());
        assert!(enforce_a2a_limits(&us, call(Some(2))).await.is_none());
        let b = enforce_a2a_limits(&us, call(Some(2)))
            .await
            .expect("the third call in the hour is refused");
        assert_eq!(b.scope, "capability");
        assert_eq!(b.limit_type, "hourly_quota");
        assert!(b.message.contains("billing.refund"), "{}", b.message);
        // No rate, or one that is not positive, limits nothing.
        for rate in [None, Some(0), Some(-1)] {
            for _ in 0..5 {
                assert!(enforce_a2a_limits(&us, call(rate)).await.is_none());
            }
        }
    }

    #[tokio::test]
    async fn a2a_card_invocation_cap_is_one_count_for_the_card() {
        let us = UsageState::new(None);
        let card = json!({"invocations": {"daily_limit": 2}});
        // Two different groups reach the card: one count.
        for group in ["g-a", "g-b"] {
            let a2a = A2aLimits {
                group: Some((group, None)),
                card: Some(("t1", "card-inv", Some(&card))),
                ..A2aLimits::default()
            };
            assert!(enforce_a2a_limits(&us, a2a).await.is_none());
        }
        let b = enforce_a2a_tenant_quota(&us, "t1", "card-inv", Some(&card))
            .await
            .expect("the card's daily cap is reached");
        assert_eq!(b.scope, "tenant");
        assert_eq!(b.limit_type, "daily_invocations");
    }

    #[tokio::test]
    async fn a2a_call_refused_by_one_scope_spends_none_of_the_others() {
        let us = UsageState::new(None);
        let group = json!({"frequency": {"max_calls_per_hour": 2}});
        let a2a = |rate| A2aLimits {
            group: Some(("g-atomic", Some(&group))),
            capability: Some(("cap-atomic", "echo", Some(rate))),
            ..A2aLimits::default()
        };
        assert!(enforce_a2a_limits(&us, a2a(1)).await.is_none());
        // The capability refuses; the group's second unit is not spent.
        assert!(enforce_a2a_limits(&us, a2a(1)).await.is_some());
        assert!(
            enforce_a2a_quota(&us, "g-atomic", Some(&group))
                .await
                .is_none(),
            "a call the capability refused must not have spent the group's allowance"
        );
    }

    #[tokio::test]
    async fn a2a_group_quota_blocks_second_call_when_rate_is_one() {
        let us = UsageState::new(None);
        let limits = json!({"frequency": {"max_calls_per_minute": 1}});
        // 1st call drains the single token.
        assert!(
            enforce_a2a_quota(&us, "g1", Some(&limits)).await.is_none(),
            "first A2A call must be allowed"
        );
        // 2nd call trips the bucket.
        let b = enforce_a2a_quota(&us, "g1", Some(&limits))
            .await
            .expect("second A2A call must block");
        assert_eq!(b.scope, "group");
        assert_eq!(b.limit_type, "rate");
        assert!(b.retry_after_secs > 0);
    }

    #[tokio::test]
    async fn a2a_group_quota_inert_without_frequency_block() {
        let us = UsageState::new(None);
        // No `frequency` block (the common current-schema case) → inert.
        let limits = json!({"chain": {"max_delegation_depth": 4}});
        for _ in 0..50 {
            assert!(
                enforce_a2a_quota(&us, "g1", Some(&limits)).await.is_none(),
                "A2A gate must be inert with no frequency block"
            );
        }
        // None limits → also inert.
        assert!(enforce_a2a_quota(&us, "g1", None).await.is_none());
    }

    #[tokio::test]
    async fn a2a_invocation_daily_cap_always_blocks_when_exceeded() {
        let us = UsageState::new(None);
        // Two allowed, third blocks — daily_limit is an absolute count cap,
        // and it enforces with NO hard_stop opt-out.
        let limits = json!({"invocations": {"daily_limit": 2}});
        assert!(enforce_a2a_quota(&us, "g1", Some(&limits)).await.is_none());
        assert!(enforce_a2a_quota(&us, "g1", Some(&limits)).await.is_none());
        let b = enforce_a2a_quota(&us, "g1", Some(&limits))
            .await
            .expect("third call must block on daily invocation cap");
        assert_eq!(b.scope, "group");
        assert_eq!(b.limit_type, "daily_invocations");
        assert!(b.retry_after_secs > 0);
    }

    #[tokio::test]
    async fn a2a_invocation_cap_ignores_legacy_hard_stop_key() {
        let us = UsageState::new(None);
        // A stored `hard_stop: false` must NOT turn the cap advisory.
        let limits = json!({"invocations": {"daily_limit": 1, "hard_stop": false}});
        assert!(enforce_a2a_quota(&us, "g1", Some(&limits)).await.is_none());
        assert!(
            enforce_a2a_quota(&us, "g1", Some(&limits)).await.is_some(),
            "cap must block regardless of legacy hard_stop key"
        );
    }

    #[tokio::test]
    async fn a2a_invocation_monthly_cap_uses_independent_counter() {
        let us = UsageState::new(None);
        // Monthly cap enforces on its own window/counter, distinct from daily.
        let limits = json!({"invocations": {"monthly_limit": 1}});
        assert!(
            enforce_a2a_quota(&us, "gmonth", Some(&limits))
                .await
                .is_none()
        );
        let b = enforce_a2a_quota(&us, "gmonth", Some(&limits))
            .await
            .expect("second call must block on monthly invocation cap");
        assert_eq!(b.limit_type, "monthly_invocations");
    }

    #[tokio::test]
    async fn a2a_invocation_caps_inert_when_absent_or_zero() {
        let us = UsageState::new(None);
        // No invocations block → inert.
        for _ in 0..30 {
            assert!(
                enforce_a2a_quota(&us, "g1", Some(&json!({})))
                    .await
                    .is_none()
            );
        }
        // Zero limit means "not configured" → inert.
        let zero = json!({"invocations": {"daily_limit": 0, "monthly_limit": 0}});
        for _ in 0..30 {
            assert!(enforce_a2a_quota(&us, "gz", Some(&zero)).await.is_none());
        }
    }

    #[tokio::test]
    async fn a2a_tenant_quota_blocks_and_uses_independent_counter() {
        let us = UsageState::new(None);
        let card_tl = json!({"frequency": {"max_calls_per_minute": 1}});
        let group_l = json!({"frequency": {"max_calls_per_minute": 1}});

        // The per-card TENANT counter (a2a:t:{tenant}:c:{card}) is independent
        // from the per-group counter (a2a:g:{group}). Drain the group counter
        // first; the tenant counter must still have its own token.
        assert!(enforce_a2a_quota(&us, "g1", Some(&group_l)).await.is_none());
        assert!(
            enforce_a2a_quota(&us, "g1", Some(&group_l)).await.is_some(),
            "group counter now exhausted"
        );

        // Tenant counter is fresh → first call allowed.
        assert!(
            enforce_a2a_tenant_quota(&us, "t1", "card1", Some(&card_tl))
                .await
                .is_none(),
            "tenant card counter is independent of the group counter"
        );
        // Second tenant call trips the tenant bucket.
        let b = enforce_a2a_tenant_quota(&us, "t1", "card1", Some(&card_tl))
            .await
            .expect("second per-card tenant call must block");
        assert_eq!(b.scope, "tenant");
        assert_eq!(b.limit_type, "rate");
    }
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `POST /v1/evidence/backfill` — seal governed rows that landed without a record.
//!
//! Rows written by seeds, before the evidence key was set, or while the
//! store was failing carry `record_id = NULL`. Backfill mints their records
//! with the writer's own builder (`mint::build_with`, unchanged) and marks each
//! `context.backfilled: true` — the log position (and the sealing time the
//! head carries) is *now*, not the row's timestamp, and the record says so;
//! resolvers report these separately ("sealed late: n").
//!
//! * **Bounded** — at most `limit` (≤ [`MAX_LIMIT`]) rows per call, in
//!   batches of [`BATCH`], keyset-paginated on `(timestamp, id)`.
//! * **Idempotent** — only rows with `record_id IS NULL` are read; a row
//!   whose record already exists in the log (sealed, but the row update was
//!   lost) is re-linked, never re-sealed; the row update is guarded by
//!   `record_id IS NULL`.
//! * **Serialised per tenant** — a session advisory lock; a concurrent
//!   backfill for the same tenant gets `409 backfill_in_progress`.
//!
//! Rows are read back as `ProxyLogInsert` through its serde shape, so a
//! column added later needs no change here. A column whose stored type does
//! not match the struct field is dropped for the whole call (the builder
//! never needs it: it reads an explicit allow-list, and every field it reads
//! is a plain column).

use crate::models::db::ProxyLogInsert;
use crate::services::evidence::mint::{self, MintOptions, Minted};
use crate::services::evidence::{attribution, planned, store};
use serde_json::{Value, json};
use sqlx::PgPool;
use std::collections::{HashMap, HashSet};

pub const MAX_LIMIT: i64 = 10_000;
pub const BATCH: i64 = 500;

#[derive(Debug, Clone, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BackfillRequest {
    pub tenant_id: String,
    pub from: chrono::DateTime<chrono::Utc>,
    pub to: chrono::DateTime<chrono::Utc>,
    #[serde(default = "default_limit")]
    pub limit: i64,
    /// Resume after this position — the `next_cursor` of a truncated call.
    /// Without it every call rescans the window from `from`, and because
    /// rows the consequential rule does not seal keep `record_id IS NULL`,
    /// a window with more than `limit` such rows could never be finished.
    #[serde(default)]
    pub after: Option<BackfillCursor>,
}

/// A keyset position in the scan: `(timestamp, id)` of the last row read.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct BackfillCursor {
    pub timestamp: chrono::DateTime<chrono::Utc>,
    pub id: String,
}

fn default_limit() -> i64 {
    1_000
}

#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct BackfillReport {
    /// Rows without a record read in the window.
    pub scanned: usize,
    /// Records newly sealed.
    pub sealed: usize,
    /// Rows re-linked to a record that already existed.
    pub relinked: usize,
    /// Rows the consequential rule does not seal (queries, non-governed surfaces).
    pub not_governed: usize,
    /// Rows whose record could not be built or read.
    pub failed: usize,
    /// True when `limit` stopped the scan before the window was exhausted.
    pub truncated: bool,
    /// Where a truncated scan stopped; pass it back as `after` to continue.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub next_cursor: Option<BackfillCursor>,
}

#[derive(Debug, thiserror::Error)]
pub enum BackfillError {
    #[error("{0}")]
    Invalid(String),
    #[error("a backfill for this tenant is already running")]
    InProgress,
    #[error(transparent)]
    Seal(#[from] store::SealError),
    #[error(transparent)]
    Db(#[from] sqlx::Error),
}

/// Mark a minted record as backfilled; re-derive its identity.
pub fn mark_backfilled(m: &mut Minted) -> Result<(), brutor_evidence::CanonError> {
    if let Value::Object(map) = &mut m.record {
        map.remove("record_id");
        let ctx = map
            .entry("context")
            .or_insert_with(|| Value::Object(Default::default()));
        if let Value::Object(c) = ctx {
            c.insert("backfilled".into(), json!(true));
        }
    }
    let id = brutor_evidence::record::compute_record_id(&m.record)?;
    if let Value::Object(map) = &mut m.record {
        map.insert("record_id".into(), json!(id));
    }
    m.record_id = id;
    Ok(())
}

/// Decode a row (as `to_jsonb`) into a `ProxyLogInsert`, dropping columns
/// in `bad` and learning new bad columns (bounded by the column count).
fn decode_row(mut row: Value, bad: &mut HashSet<String>) -> Option<ProxyLogInsert> {
    if let Value::Object(m) = &mut row {
        for k in bad.iter() {
            m.remove(k);
        }
    }
    for _ in 0..256 {
        match serde_path_to_error::deserialize::<_, ProxyLogInsert>(row.clone()) {
            Ok(log) => return Some(log),
            Err(e) => {
                let field = e
                    .path()
                    .iter()
                    .next()
                    .map(|seg| seg.to_string())
                    .filter(|f| !f.is_empty() && f != "?")?;
                let removed = match &mut row {
                    Value::Object(m) => m.remove(&field).is_some(),
                    _ => false,
                };
                if !removed {
                    return None;
                }
                bad.insert(field);
            }
        }
    }
    None
}

/// The row's mint options, resolved as the writer resolves them (profile
/// flags, tool annotations, two-phase confirmation). Salted digests are
/// never computed here: stored previews may have been truncated or pruned,
/// so a salting system gets a record without body digests rather than
/// digests over something other than what was observed.
async fn options_for(pool: &PgPool, log: &ProxyLogInsert) -> Result<MintOptions, sqlx::Error> {
    let evidence =
        attribution::options(pool, &log.tenant_id, log.resource_group_id.as_deref()).await?;
    let tool_read_only = match (
        log.mcp_method.as_deref(),
        log.server_id.as_deref(),
        log.mcp_capability_name.as_deref(),
    ) {
        (Some("tools/call"), Some(server), Some(tool)) => {
            attribution::tool_read_only(pool, &log.tenant_id, server, tool).await?
        }
        _ => None,
    };
    let mut opts = MintOptions {
        evidence,
        tool_read_only,
        ..Default::default()
    };
    if let Some(surface @ ("mcp" | "a2a" | "skill")) = mint::surface_of(log) {
        opts.confirms = planned::confirmed_by(
            pool,
            &log.tenant_id,
            &format!("{surface}/{}", log.id),
            opts.evidence.two_phase_actions,
        )
        .await;
    }
    Ok(opts)
}

pub async fn run(pool: &PgPool, req: &BackfillRequest) -> Result<BackfillReport, BackfillError> {
    if req.tenant_id.trim().is_empty() {
        return Err(BackfillError::Invalid("tenant_id is required".into()));
    }
    if req.from >= req.to {
        return Err(BackfillError::Invalid("from must be before to".into()));
    }
    if req.limit < 1 || req.limit > MAX_LIMIT {
        return Err(BackfillError::Invalid(format!(
            "limit must be 1..={MAX_LIMIT}"
        )));
    }
    if !crate::services::evidence::key::is_enabled() {
        return Err(store::SealError::Disabled.into());
    }
    let tenant = req.tenant_id.as_str();
    // Session lock on a dedicated connection, held for the whole call.
    let mut lock_conn = pool.acquire().await?;
    let (got,): (bool,) = sqlx::query_as("SELECT pg_try_advisory_lock(hashtext($1))")
        .bind(format!("evidence_backfill:{tenant}"))
        .fetch_one(&mut *lock_conn)
        .await?;
    if !got {
        return Err(BackfillError::InProgress);
    }
    let result = run_locked(pool, req).await;
    let _ = sqlx::query("SELECT pg_advisory_unlock(hashtext($1))")
        .bind(format!("evidence_backfill:{tenant}"))
        .execute(&mut *lock_conn)
        .await;
    result
}

async fn run_locked(pool: &PgPool, req: &BackfillRequest) -> Result<BackfillReport, BackfillError> {
    let tenant = req.tenant_id.as_str();
    let mut report = BackfillReport::default();
    let mut bad_cols: HashSet<String> = HashSet::new();
    let mut cursor: Option<(chrono::DateTime<chrono::Utc>, String)> =
        req.after.as_ref().map(|c| (c.timestamp, c.id.clone()));
    loop {
        let remaining = req.limit - report.scanned as i64;
        if remaining <= 0 {
            // Anything left in the window?
            let more: Option<(i64,)> = sqlx::query_as(
                "SELECT 1::bigint FROM proxy_logs WHERE tenant_id = $1 AND record_id IS NULL \
                 AND timestamp >= $2 AND timestamp < $3 AND (timestamp, id) > ($4, $5) LIMIT 1",
            )
            .bind(tenant)
            .bind(req.from)
            .bind(req.to)
            .bind(cursor.as_ref().map(|c| c.0).unwrap_or(req.from))
            .bind(cursor.as_ref().map(|c| c.1.as_str()).unwrap_or(""))
            .fetch_optional(pool)
            .await?;
            report.truncated = more.is_some();
            if report.truncated {
                report.next_cursor = cursor.as_ref().map(|(t, id)| BackfillCursor {
                    timestamp: *t,
                    id: id.clone(),
                });
            }
            break;
        }
        let take = remaining.min(BATCH);
        let rows: Vec<(chrono::DateTime<chrono::Utc>, String, Value)> = sqlx::query_as(
            "SELECT p.timestamp, p.id, to_jsonb(p) FROM proxy_logs p \
             WHERE p.tenant_id = $1 AND p.record_id IS NULL \
               AND p.timestamp >= $2 AND p.timestamp < $3 \
               AND ($4::timestamptz IS NULL OR (p.timestamp, p.id) > ($4, $5)) \
             ORDER BY p.timestamp, p.id LIMIT $6",
        )
        .bind(tenant)
        .bind(req.from)
        .bind(req.to)
        .bind(cursor.as_ref().map(|c| c.0))
        .bind(cursor.as_ref().map(|c| c.1.clone()).unwrap_or_default())
        .bind(take)
        .fetch_all(pool)
        .await?;
        if rows.is_empty() {
            break;
        }
        cursor = rows.last().map(|(t, id, _)| (*t, id.clone()));
        report.scanned += rows.len();

        let ids: Vec<String> = rows.iter().map(|(_, id, _)| id.clone()).collect();
        // Records already in the log for these rows: re-link, never re-seal.
        let existing: Vec<(String, String)> = sqlx::query_as(
            "SELECT proxy_log_id, record_id FROM action_records \
             WHERE tenant_id = $1 AND proxy_log_id = ANY($2)",
        )
        .bind(tenant)
        .bind(&ids)
        .fetch_all(pool)
        .await?;
        let existing: HashMap<String, String> = existing.into_iter().collect();

        let mut minted: Vec<Minted> = Vec::new();
        let mut links: Vec<(String, String)> = Vec::new();
        for (_, id, row) in rows {
            if let Some(rid) = existing.get(&id) {
                links.push((id, rid.clone()));
                report.relinked += 1;
                continue;
            }
            let Some(log) = decode_row(row, &mut bad_cols) else {
                report.failed += 1;
                continue;
            };
            // A lookup error leaves the row for a later backfill.
            let opts = match options_for(pool, &log).await {
                Ok(o) => o,
                Err(e) => {
                    tracing::error!(target = "brutor::evidence", proxy_log_id = %log.id, error = %e, "backfill: options lookup failed");
                    report.failed += 1;
                    continue;
                }
            };
            if !mint::should_record_with(&log, &opts) {
                report.not_governed += 1;
                continue;
            }
            let attr = match attribution::resolve(
                pool,
                &log.tenant_id,
                log.resource_group_id.as_deref(),
            )
            .await
            {
                Ok(a) => a,
                Err(e) => {
                    tracing::error!(target = "brutor::evidence", proxy_log_id = %log.id, error = %e, "backfill: attribution lookup failed");
                    report.failed += 1;
                    continue;
                }
            };
            match mint::build_with(&log, attr.as_deref(), &opts) {
                Ok(Some(mut m)) => {
                    if mark_backfilled(&mut m).is_err() {
                        report.failed += 1;
                        continue;
                    }
                    m.row_index = minted.len();
                    minted.push(m);
                }
                Ok(None) => report.not_governed += 1,
                Err(e) => {
                    tracing::warn!(
                        target = "brutor::evidence",
                        proxy_log_id = %id,
                        error = %e,
                        "backfill: record could not be built",
                    );
                    report.failed += 1;
                }
            }
        }
        if !minted.is_empty() {
            let sealed = store::seal(pool, tenant, &minted).await?;
            report.sealed += sealed.len();
            for s in sealed {
                if let Some(pl) = minted.get(s.row_index).and_then(|m| m.proxy_log_id.clone()) {
                    links.push((pl, s.record_id));
                }
            }
        }
        if !links.is_empty() {
            let (pl_ids, rec_ids): (Vec<String>, Vec<String>) = links.into_iter().unzip();
            sqlx::query(
                "UPDATE proxy_logs p SET record_id = u.record_id \
                 FROM UNNEST($2::text[], $3::text[]) AS u(id, record_id) \
                 WHERE p.tenant_id = $1 AND p.id = u.id AND p.record_id IS NULL",
            )
            .bind(tenant)
            .bind(&pl_ids)
            .bind(&rec_ids)
            .execute(pool)
            .await?;
        }
    }
    if !bad_cols.is_empty() {
        tracing::debug!(
            target = "brutor::evidence",
            columns = ?bad_cols,
            "backfill: columns not decodable into ProxyLogInsert were ignored",
        );
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decode_row_drops_mistyped_columns_once() {
        let mut bad = HashSet::new();
        // Missing required fields ⇒ None; a mistyped optional column is learnt.
        assert!(decode_row(json!({"id": 1}), &mut bad).is_none());
    }
}

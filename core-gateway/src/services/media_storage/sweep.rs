// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
//! Media files GC sweep.
//!
//! Two passes, both safe to run concurrently with portal/upload
//! traffic:
//!
//! 1. **Soft-delete pass**: rows whose retention deadline
//!    (`expires_at`) has elapsed AND aren't already deleted are
//!    flipped — `deleted_at = NOW()`. Bytes still live in storage,
//!    so an operator can restore by clearing the column. The portal
//!    refuses signed-URL issuance immediately (the `is_alive` check
//!    in `routes::files::get_signed_url`).
//!
//! 2. **Hard-delete pass**: rows soft-deleted more than 7 days ago
//!    have their object removed from storage AND their row deleted.
//!    7 days is the documented recovery window.
//!
//! ## Safety properties
//!
//! - **Idempotent**: a partial run that crashes between the storage
//!   delete and the row delete leaves the bytes already gone (S3
//!   delete is idempotent; the trait contract requires the same of
//!   any backend) and the next sweep removes the row. We never
//!   leave bytes pointing at no row.
//! - **Bounded per pass**: each pass processes at most
//!   `MAX_BATCH` rows so a misconfigured retention storm can't lock
//!   up the table for minutes. The next tick picks up the next
//!   batch.
//! - **Best-effort storage delete**: a missing object is not an
//!   error — we log and proceed to remove the row. This protects
//!   us from a half-completed previous run.
//!
//! ## Cadence
//!
//! Every 6 hours (`config::fixed::MEDIA_GC_INTERVAL_SECS`). Note we don't run the sweep at boot — it would race
//! with health checks on a fresh node and the next tick is fine.

use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Utc};
use sqlx::PgPool;

use super::storage::{Storage, StorageError};

/// Maximum rows touched per pass. Keeps locks short and lets the
/// next tick continue making progress on a backlog.
pub const MAX_BATCH: i64 = 500;

/// Recovery window — how long a soft-deleted row sits before its
/// bytes are hard-deleted.
pub const HARD_DELETE_AGE_DAYS: i64 = 7;

/// Default interval between sweeps. Six hours is a quiet cadence
/// that still moves through a tenant's daily churn well within a
/// week.
pub const DEFAULT_INTERVAL_SECS: u64 = 6 * 60 * 60;

/// Outcome of a single sweep pass — returned for observability.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SweepReport {
    pub soft_deleted: u64,
    pub hard_deleted: u64,
    pub storage_failures: u64,
    pub db_failures: u64,
}

/// Run one full sweep cycle. Logs summary fields when something
/// happens; silent when the table is clean.
///
/// Pulled out of the spawn loop so unit/integration tests can
/// exercise it directly against a `PgPool` and an
/// `Arc<dyn Storage>`.
pub async fn run_once(db: &PgPool, storage: Arc<dyn Storage>) -> SweepReport {
    let mut report = SweepReport::default();

    // ---- Pass 1: flip past-retention rows to soft-deleted ----
    let soft = soft_delete_expired(db).await;
    report.soft_deleted = soft;

    // ---- Pass 2: hard-delete the bytes + rows past the window ----
    let cutoff: DateTime<Utc> = Utc::now() - chrono::Duration::days(HARD_DELETE_AGE_DAYS);
    let candidates: Vec<(String, String)> = match sqlx::query_as(
        r#"
        SELECT id, object_key
        FROM media_files
        WHERE deleted_at IS NOT NULL
          AND deleted_at < $1
        ORDER BY deleted_at ASC
        LIMIT $2
        "#,
    )
    .bind(cutoff)
    .bind(MAX_BATCH)
    .fetch_all(db)
    .await
    {
        Ok(rows) => rows,
        Err(err) => {
            tracing::warn!("media GC: SELECT hard-delete candidates failed: {err}");
            return report;
        }
    };

    for (file_id, object_key) in candidates {
        // Best-effort storage delete. NotFound is fine — somebody
        // already cleaned the object, or it never made it (write
        // crashed mid-upload). Other errors are logged but don't
        // block row removal: the alternative is leaving the row
        // forever and never reclaiming the file_id.
        match storage.delete(&object_key).await {
            Ok(()) => {}
            Err(StorageError::NotFound { .. }) => {
                tracing::debug!(
                    object_key = %object_key,
                    "media GC: object already missing — proceeding to row delete"
                );
            }
            Err(err) => {
                tracing::warn!(
                    file_id = %file_id,
                    object_key = %object_key,
                    "media GC: storage delete failed: {err}"
                );
                report.storage_failures += 1;
                // Skip the row delete — try again next tick.
                continue;
            }
        }

        let res = sqlx::query("DELETE FROM media_files WHERE id = $1")
            .bind(&file_id)
            .execute(db)
            .await;
        match res {
            Ok(_) => report.hard_deleted += 1,
            Err(err) => {
                tracing::warn!(
                    file_id = %file_id,
                    "media GC: row DELETE failed: {err}"
                );
                report.db_failures += 1;
            }
        }
    }

    if report.soft_deleted > 0
        || report.hard_deleted > 0
        || report.storage_failures > 0
        || report.db_failures > 0
    {
        tracing::info!(
            soft_deleted = report.soft_deleted,
            hard_deleted = report.hard_deleted,
            storage_failures = report.storage_failures,
            db_failures = report.db_failures,
            "media GC sweep completed"
        );
    }
    report
}

async fn soft_delete_expired(db: &PgPool) -> u64 {
    let res = sqlx::query(
        r#"
        UPDATE media_files
        SET deleted_at = NOW()
        WHERE id IN (
            SELECT id FROM media_files
            WHERE deleted_at IS NULL
              AND expires_at < NOW()
            ORDER BY expires_at ASC
            LIMIT $1
        )
        "#,
    )
    .bind(MAX_BATCH)
    .execute(db)
    .await;

    match res {
        Ok(r) => r.rows_affected(),
        Err(err) => {
            tracing::warn!("media GC: soft-delete UPDATE failed: {err}");
            0
        }
    }
}

/// Spawn the sweep loop. Returns the JoinHandle so the caller can
/// keep ownership; the task only exits on shutdown.
pub fn spawn_loop(
    db: PgPool,
    storage: Arc<dyn Storage>,
    interval_secs: u64,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let secs = interval_secs.max(60);
        let mut ticker = tokio::time::interval(Duration::from_secs(secs));
        // Skip the immediate first tick — boot races with other
        // services and we don't want to add load there.
        ticker.tick().await;
        loop {
            ticker.tick().await;
            let _ = run_once(&db, storage.clone()).await;
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::media_storage::InMemoryStorage;
    use bytes::Bytes;

    /// SQL-side tests need a real Postgres. They are skipped unless
    /// the `BRUTOR_PG_TEST_URL` env var points at a usable
    /// database — same convention as the rest of the integration
    /// tests in this crate.
    fn pg_url_from_env() -> Option<String> {
        std::env::var("BRUTOR_PG_TEST_URL")
            .ok()
            .filter(|s| !s.is_empty())
    }

    async fn fresh_pool() -> Option<PgPool> {
        let url = pg_url_from_env()?;
        sqlx::PgPool::connect(&url).await.ok()
    }

    /// Smoke-tests the sweep pipeline with a real Postgres + an
    /// in-memory storage. Verifies:
    ///   - past-retention undeleted row gets flipped soft-deleted
    ///   - row soft-deleted >7 days ago gets hard-deleted (row +
    ///     bytes both gone)
    ///   - row soft-deleted <7 days ago is left alone
    #[tokio::test]
    async fn sweep_flips_and_hard_deletes_per_window() {
        let db = match fresh_pool().await {
            Some(p) => p,
            None => {
                eprintln!("[skip] BRUTOR_PG_TEST_URL not set; skipping sweep integration test.");
                return;
            }
        };

        // Seed three rows (we use a unique tenant so we can
        // clean up without colliding with other tests). Tenant id
        // must satisfy the model's `[a-z0-9][a-z0-9-_]*[a-z0-9]`
        // validator — lowercase only — so we lowercase the ULID.
        let tenant_id = format!(
            "sweep-test-{}",
            ulid::Ulid::new().to_string().to_lowercase()
        );
        sqlx::query(
            "INSERT INTO tenants (
                 id, name, display_name, primary_contact_email,
                 is_active, is_default, is_trial, suspended,
                 features, settings, mfa_policy
             )
             VALUES (
                 $1, $1, $1, 'sweep@test',
                 TRUE, FALSE, FALSE, FALSE,
                 '{}'::json, '{}'::json, 'optional'
             )
             ON CONFLICT (id) DO NOTHING",
        )
        .bind(&tenant_id)
        .execute(&db)
        .await
        .expect("seed tenant");

        let storage: Arc<dyn Storage> = Arc::new(InMemoryStorage::new());

        // Row A — past retention, not soft-deleted yet → should be flipped.
        let a_id = format!("file_{}", ulid::Ulid::new());
        let a_key = format!("{}/image/2026/04/{}.png", tenant_id, a_id);
        storage
            .put(&a_key, Bytes::from_static(b"a"), "image/png")
            .await
            .unwrap();
        sqlx::query(
            "INSERT INTO media_files (id, tenant_id, kind, mime_type, size_bytes,
             object_key, created_at, expires_at)
             VALUES ($1, $2, 'image', 'image/png', 1, $3, NOW() - INTERVAL '40 days',
                     NOW() - INTERVAL '1 hour')",
        )
        .bind(&a_id)
        .bind(&tenant_id)
        .bind(&a_key)
        .execute(&db)
        .await
        .unwrap();

        // Row B — soft-deleted 8 days ago → should hard-delete.
        let b_id = format!("file_{}", ulid::Ulid::new());
        let b_key = format!("{}/image/2026/04/{}.png", tenant_id, b_id);
        storage
            .put(&b_key, Bytes::from_static(b"b"), "image/png")
            .await
            .unwrap();
        sqlx::query(
            "INSERT INTO media_files (id, tenant_id, kind, mime_type, size_bytes,
             object_key, created_at, expires_at, deleted_at)
             VALUES ($1, $2, 'image', 'image/png', 1, $3, NOW() - INTERVAL '40 days',
                     NOW() - INTERVAL '10 days', NOW() - INTERVAL '8 days')",
        )
        .bind(&b_id)
        .bind(&tenant_id)
        .bind(&b_key)
        .execute(&db)
        .await
        .unwrap();

        // Row C — soft-deleted 1 day ago → must be left alone (still
        // inside the 7-day recovery window).
        let c_id = format!("file_{}", ulid::Ulid::new());
        let c_key = format!("{}/image/2026/04/{}.png", tenant_id, c_id);
        storage
            .put(&c_key, Bytes::from_static(b"c"), "image/png")
            .await
            .unwrap();
        sqlx::query(
            "INSERT INTO media_files (id, tenant_id, kind, mime_type, size_bytes,
             object_key, created_at, expires_at, deleted_at)
             VALUES ($1, $2, 'image', 'image/png', 1, $3, NOW() - INTERVAL '40 days',
                     NOW() - INTERVAL '5 days', NOW() - INTERVAL '1 day')",
        )
        .bind(&c_id)
        .bind(&tenant_id)
        .bind(&c_key)
        .execute(&db)
        .await
        .unwrap();

        let report = run_once(&db, storage.clone()).await;
        assert!(report.soft_deleted >= 1, "row A should soft-delete");
        assert!(report.hard_deleted >= 1, "row B should hard-delete");

        // Row A: now soft-deleted, bytes still present (waiting for window).
        let a_alive: (Option<chrono::DateTime<Utc>>,) =
            sqlx::query_as("SELECT deleted_at FROM media_files WHERE id = $1")
                .bind(&a_id)
                .fetch_one(&db)
                .await
                .unwrap();
        assert!(a_alive.0.is_some(), "row A deleted_at should be set");
        assert!(
            storage.get(&a_key).await.is_ok(),
            "row A bytes should still be in storage"
        );

        // Row B: row gone + bytes gone.
        let b_count: (i64,) = sqlx::query_as("SELECT COUNT(*) FROM media_files WHERE id = $1")
            .bind(&b_id)
            .fetch_one(&db)
            .await
            .unwrap();
        assert_eq!(b_count.0, 0, "row B should be removed from media_files");
        assert!(
            matches!(
                storage.get(&b_key).await,
                Err(StorageError::NotFound { .. })
            ),
            "row B bytes should be removed from storage"
        );

        // Row C: untouched.
        let c_alive: (Option<chrono::DateTime<Utc>>,) =
            sqlx::query_as("SELECT deleted_at FROM media_files WHERE id = $1")
                .bind(&c_id)
                .fetch_one(&db)
                .await
                .unwrap();
        assert!(
            c_alive.0.is_some(),
            "row C deleted_at should still be set (was already)"
        );
        assert!(
            storage.get(&c_key).await.is_ok(),
            "row C bytes should be left alone — inside recovery window"
        );

        // Cleanup
        let _ = sqlx::query("DELETE FROM media_files WHERE tenant_id = $1")
            .bind(&tenant_id)
            .execute(&db)
            .await;
        let _ = sqlx::query("DELETE FROM tenants WHERE id = $1")
            .bind(&tenant_id)
            .execute(&db)
            .await;
    }
}

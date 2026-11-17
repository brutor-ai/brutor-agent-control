// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The background writer's evidence hook.
//!
//! Two calls from `background_writer::flush_logs`:
//!
//! 1. [`prepare`] — after the content digests are sealed and **before**
//!    the tenant's `store_bodies` mode truncates, redacts or drops the
//!    bodies. Resolves each row's options (profile flags, tool annotations,
//!    RG sensitivity — all process-cached) and, where the system salts its
//!    digests, computes the salted digests from the bodies as captured: the
//!    same value the row's digests commit to, never available again after
//!    retention runs.
//! 2. [`mint_and_store`] — after the batch is hash-stamped: build the
//!    records (citing the row hash), chain two-phase outcomes to their
//!    `planned` records and inference records to their conversation's
//!    notice, persist per tenant, and stamp `record_id` onto the rows before
//!    the INSERT.
//!
//! Body previews are read here for one purpose only — to digest them. They
//! never reach `mint`, which cannot see them.

use crate::models::db::ProxyLogInsert;
use crate::services::evidence::mint::{self, MintOptions, SaltedDigests};
use crate::services::evidence::{attribution, key, metrics, planned, store};
use sqlx::PgPool;
use std::collections::HashMap;

/// Per-row preparation, indexed like the batch. `None` = not an evidence row.
#[derive(Debug, Default)]
pub struct Prepared {
    rows: HashMap<usize, MintOptions>,
    ready: bool,
}

/// A lookup error is returned; the caller counts a
/// `lookup_failed` capture gap and leaves the row for backfill.
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
    Ok(MintOptions {
        evidence,
        tool_read_only,
        ..Default::default()
    })
}

/// Count and log one row left unsealed because a lookup failed.
fn lookup_gap(log: &ProxyLogInsert, what: &str, e: &sqlx::Error) {
    metrics::capture_gap(metrics::gap_reason::LOOKUP_FAILED, 1);
    tracing::error!(
        target = "brutor::evidence",
        proxy_log_id = %log.id,
        lookup = what,
        error = %e,
        "evidence lookup failed; row left unsealed for backfill",
    );
}

/// Resolve options and capture salted digests while the bodies are whole.
pub async fn prepare(pool: &PgPool, batch: &[ProxyLogInsert]) -> Prepared {
    let mut out = Prepared {
        rows: HashMap::new(),
        ready: true,
    };
    if !key::is_enabled() {
        return out;
    }
    for (i, log) in batch.iter().enumerate() {
        if mint::surface_of(log).is_none() {
            continue;
        }
        let mut opts = match options_for(pool, log).await {
            Ok(o) => o,
            Err(e) => {
                lookup_gap(log, "options", &e);
                continue;
            }
        };
        if !mint::should_record_with(log, &opts) {
            continue;
        }
        if opts.evidence.salt_digests {
            opts.salted = Some(SaltedDigests::compute(
                log.request_body_preview.as_deref(),
                log.response_body_preview.as_deref(),
            ));
        }
        out.rows.insert(i, opts);
    }
    out
}

/// Mint records for every qualifying row in `batch`, persist them per
/// tenant, and stamp `record_id` onto the rows. Best-effort: a failure
/// costs the records for one tenant's slice of one batch, is logged and
/// counted (`brutor_evidence_capture_gap_total`); the rows still land and
/// the missing `record_id` is what the `records.every_verdict_sealed`
/// resolver reports.
///
/// `prepared = None` (callers that never saw the bodies) resolves options
/// here; a system that salts then gets records without digests rather than
/// with unsalted ones.
pub async fn mint_and_store(
    pool: &PgPool,
    batch: &mut [ProxyLogInsert],
    prepared: Option<Prepared>,
) {
    if !key::is_enabled() {
        let n = batch.iter().filter(|l| mint::should_record(l)).count();
        metrics::capture_gap(metrics::gap_reason::DISABLED, n as u64);
        return;
    }
    let mut prepared = prepared.unwrap_or_default();
    let mut minted: Vec<mint::Minted> = Vec::new();
    let mut build_failed = 0u64;
    for (i, log) in batch.iter().enumerate() {
        let mut opts = if prepared.ready {
            match prepared.rows.remove(&i) {
                Some(o) => o,
                None => continue,
            }
        } else {
            if mint::surface_of(log).is_none() {
                continue;
            }
            let o = match options_for(pool, log).await {
                Ok(o) => o,
                Err(e) => {
                    lookup_gap(log, "options", &e);
                    continue;
                }
            };
            if !mint::should_record_with(log, &o) {
                continue;
            }
            o
        };
        let surface = mint::surface_of(log).unwrap_or_default();
        if matches!(surface, "mcp" | "a2a" | "skill") {
            opts.confirms = planned::confirmed_by(
                pool,
                &log.tenant_id,
                &format!("{surface}/{}", log.id),
                opts.evidence.two_phase_actions,
            )
            .await;
        }
        if surface == "llm"
            && let Some(handle) = log
                .custom_metadata
                .as_ref()
                .and_then(|m| m.get("conversation_handle"))
                .and_then(|v| v.as_str())
                .and_then(crate::services::evidence::request::parse_conversation_handle)
        {
            opts.notice_record_id =
                match attribution::notice_for(pool, &log.tenant_id, &handle).await {
                    Ok(n) => n,
                    Err(e) => {
                        lookup_gap(log, "notice", &e);
                        continue;
                    }
                };
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
                lookup_gap(log, "attribution", &e);
                continue;
            }
        };
        match mint::build_with(log, attr.as_deref(), &opts) {
            Ok(Some(m)) => minted.push(mint::Minted { row_index: i, ..m }),
            Ok(None) => {}
            Err(e) => {
                build_failed += 1;
                tracing::error!(
                    target = "brutor::evidence",
                    proxy_log_id = %log.id,
                    error = %e,
                    "action record could not be built; row will land without record_id",
                );
            }
        }
    }
    metrics::capture_gap(metrics::gap_reason::BUILD_FAILED, build_failed);
    if minted.is_empty() {
        return;
    }
    let ids = store::write_batch(pool, &minted).await;
    metrics::capture_gap(
        metrics::gap_reason::SEAL_FAILED,
        (minted.len() - ids.len()) as u64,
    );
    let now = chrono::Utc::now();
    for (row_index, record_id) in ids {
        if let Some(log) = batch.get_mut(row_index) {
            let lag = (now - log.timestamp).num_milliseconds() as f64 / 1000.0;
            metrics::record_lag(mint::surface_of(log).unwrap_or("other"), lag);
            log.record_id = Some(record_id);
        }
    }
}

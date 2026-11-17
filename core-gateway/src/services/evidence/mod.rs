// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Sealed action records — the evidence layer.
//!
//! Every governed verdict that reaches the audit writer is rendered as an
//! `ai.brutor/action-record/v1` record, sealed as an RFC 9943 Signed
//! Statement under the deployment's evidence key, and appended as a leaf
//! to the tenant's RFC 6962 Merkle log. The proxy-log row and the record
//! point at each other (`proxy_logs.record_id` ↔ `references[]` carrying
//! the row hash), and the record's digests *are* the row's content
//! digests, so the two ledgers cannot disagree.
//!
//! Layout:
//! * [`key`] — the evidence signing key and issuer (env-loaded, like the
//!   audit key; absent key ⇒ no records, never an outage).
//! * [`attribution`] — cached (tenant, resource group) → AI System +
//!   active contract lookup, the same shape as `compliance::load_for_audit`.
//! * [`mint`] — `ProxyLogInsert` → record, including the verdict mapping
//!   and the data-admission allow-list. Never reads a never-enters field.
//! * [`store`] — the per-tenant transaction: advisory lock, append leaves,
//!   persist records, nodes, entries and the log head.
//!
//! The writer calls [`mint_and_store`] after the batch is hash-stamped and
//! before the rows are inserted, so the record can cite the row hash and
//! the row can carry the record id in the same INSERT.

pub mod approval;
pub mod attribution;
pub mod backfill;
pub mod bundle;
pub mod chain_head;
pub mod heads;
pub mod key;
pub mod metrics;
pub mod mint;
pub mod notice;
pub mod planned;
pub mod request;
pub mod seal_api;
pub mod store;
pub mod witness;
pub mod writer;

use crate::models::db::ProxyLogInsert;
use sqlx::PgPool;

/// Mint records for every qualifying row in `batch`, persist them per
/// tenant, and stamp `record_id` onto the rows. See [`writer`]; the
/// background writer calls [`writer::prepare`] before retention and
/// [`writer::mint_and_store`] after stamping. This entry point is for
/// callers that hold no pre-retention bodies (e.g. a backfill).
pub async fn mint_and_store(pool: &PgPool, batch: &mut [ProxyLogInsert]) {
    writer::mint_and_store(pool, batch, None).await;
}

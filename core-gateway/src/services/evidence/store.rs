// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Persisting minted records into the tenant's Merkle log.
//!
//! One transaction per tenant per batch:
//!
//! 1. `pg_advisory_xact_lock(hashtext('evidence_log:' || tenant))` — the
//!    single-writer rule across replicas, scoped to the transaction so a
//!    crash releases it.
//! 2. Load (or create) the `evidence_logs` row and the tree's peaks — the
//!    only nodes an append needs.
//! 3. Attach chains that need a lookup (`awaiting_human` parents).
//! 4. Seal each record as a Signed Statement, hash the statement bytes as
//!    the leaf, append, and insert the record, its log entry and the
//!    nodes the append completed.
//! 5. Update the log head. Commit.
//!
//! Failure anywhere rolls the tenant's slice back; the rows still land and
//! their missing `record_id` is the visible capture gap.

use crate::services::evidence::heads::{self, IssuedHead};
use crate::services::evidence::mint::Minted;
use crate::services::evidence::{chain_head, key};
use brutor_cose::merkle::{Hash, leaf_hash};
use brutor_ctlog::{Log, MemoryStore, NodeStore, RecordingStore, peaks_for};
use brutor_evidence::record::{Chain, relation};
use brutor_evidence::statement::seal_record;
use ed25519_dalek::SigningKey;
use serde_json::{Value, json};
use sqlx::{PgPool, Postgres, Transaction};
use std::collections::HashMap;

/// One record as it landed in the tenant log.
#[derive(Debug, Clone)]
pub struct Sealed {
    /// The caller's index (`Minted::row_index`) — the writer's row slot, or
    /// the position in a standalone request.
    pub row_index: usize,
    pub record_id: String,
    pub log_id: String,
    pub seq: i64,
    /// The RFC 9943 Signed Statement bytes (COSE_Sign1).
    pub statement: Vec<u8>,
    /// The record as stored (a looked-up chain may have been attached).
    pub record: Value,
}

/// Seal records that did not come from a proxy-log row — declarations,
/// epoch boundaries, notices, assessments, reports, incident changes —
/// into `tenant`'s log, at write.
///
/// Same transaction, lock, chain lookup and head cadence as the writer's
/// path; unlike [`write_batch`] the error is returned, because the caller
/// is an API request that must not claim a declaration was sealed when it
/// was not. `Minted::row_index` is echoed back in each [`Sealed`].
pub async fn seal(pool: &PgPool, tenant: &str, items: &[Minted]) -> Result<Vec<Sealed>, SealError> {
    let signing = key::signing_key_for(pool, tenant)
        .await?
        .ok_or(SealError::Disabled)?;
    let refs: Vec<Item<'_>> = items.iter().map(Item::record).collect();
    Ok(write_tenant(pool, tenant, &refs, &signing, false).await?.0)
}

/// [`seal`] with an explicit key and entry kinds — the rotation record
/// (signed by the *old* key) and disclosure entries (kind `disclosure`).
pub(crate) async fn seal_items(
    pool: &PgPool,
    tenant: &str,
    items: &[Item<'_>],
    signing: &SigningKey,
) -> Result<Vec<Sealed>, SealError> {
    Ok(write_tenant(pool, tenant, items, signing, false).await?.0)
}

/// Force a tree head if anything is uncovered
/// (`POST /v1/evidence/logs/{tenant}/tree-head`, and the bundle endpoint
/// before it proves anything). `Ok(None)` when the log is empty or already
/// covered: a head is never re-signed over an unchanged log.
pub async fn force_head(pool: &PgPool, tenant: &str) -> Result<Option<IssuedHead>, SealError> {
    let signing = key::signing_key_for(pool, tenant)
        .await?
        .ok_or(SealError::Disabled)?;
    let exists: Option<(i64,)> =
        sqlx::query_as("SELECT tree_size FROM evidence_logs WHERE tenant_id = $1")
            .bind(tenant)
            .fetch_optional(pool)
            .await?;
    if exists.is_none() {
        return Ok(None);
    }
    Ok(write_tenant(pool, tenant, &[], &signing, true).await?.1)
}

/// One leaf to append: a record and the entry kind it lands as.
pub(crate) struct Item<'a> {
    pub minted: &'a Minted,
    /// `record` (default) or `disclosure`.
    pub entry_kind: &'static str,
    /// `evidence_log_entries.ref_id`; defaults to the record id.
    pub ref_id: Option<String>,
}

impl<'a> Item<'a> {
    pub fn record(m: &'a Minted) -> Self {
        Self {
            minted: m,
            entry_kind: "record",
            ref_id: None,
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum SealError {
    #[error(
        "evidence signing key is not configured ({})",
        crate::env_vars::BRUTOR_EVIDENCE_SIGNING_KEY.name
    )]
    Disabled,
    #[error(transparent)]
    Db(#[from] sqlx::Error),
    /// The tenant signs with its own key and that key cannot be used:
    /// nothing is sealed under another one.
    #[error(transparent)]
    TenantKey(#[from] key::TenantKeyError),
}

/// Persist records; returns `(row_index, record_id)` for every record that
/// landed so the writer can stamp the rows.
pub async fn write_batch(pool: &PgPool, minted: &[Minted]) -> Vec<(usize, String)> {
    if !key::is_enabled() {
        return vec![];
    }
    let mut by_tenant: HashMap<&str, Vec<&Minted>> = HashMap::new();
    for m in minted {
        by_tenant.entry(m.tenant_id.as_str()).or_default().push(m);
    }
    let mut landed = Vec::with_capacity(minted.len());
    for (tenant, items) in by_tenant {
        // `Err`: the tenant's own key is unusable (logged by the resolver).
        // Its rows land without a record and are backfilled later; they are
        // never sealed under the deployment key.
        let Ok(Some(signing)) = key::signing_key_for(pool, tenant).await else {
            continue;
        };
        let refs: Vec<Item<'_>> = items.iter().map(|m| Item::record(m)).collect();
        match write_tenant(pool, tenant, &refs, &signing, false).await {
            Ok((sealed, _)) => {
                landed.extend(sealed.into_iter().map(|s| (s.row_index, s.record_id)))
            }
            Err(e) => tracing::warn!(
                target = "brutor::evidence",
                tenant_id = %tenant,
                records = items.len(),
                error = %e,
                "evidence records not persisted for this batch; rows land without record_id",
            ),
        }
    }
    landed
}

/// The open tenant log inside a transaction: its id, size and a node store
/// seeded with the peaks.
pub(crate) struct OpenLog {
    pub log_id: String,
    pub size: u64,
    pub store: RecordingStore<MemoryStore>,
}

/// Take the tenant lock, load (or create) the log row and preload the
/// peaks — the only nodes an append needs.
pub(crate) async fn open_log(
    tx: &mut Transaction<'_, Postgres>,
    tenant: &str,
    kid: &str,
) -> Result<OpenLog, sqlx::Error> {
    sqlx::query("SELECT pg_advisory_xact_lock(hashtext($1))")
        .bind(format!("evidence_log:{tenant}"))
        .execute(&mut **tx)
        .await?;
    // The stored id wins: a log's identity is fixed at creation (see
    // `key::log_id_for`), so the lookup is by tenant, under the lock.
    let row: Option<(String, i64, i64)> = sqlx::query_as(
        "SELECT log_id, next_seq, tree_size FROM evidence_logs WHERE tenant_id = $1 FOR UPDATE",
    )
    .bind(tenant)
    .fetch_optional(&mut **tx)
    .await?;
    let log_id = row
        .as_ref()
        .map(|r| r.0.clone())
        .unwrap_or_else(|| key::log_id_for(tenant));
    let (next_seq, tree_size) = match row {
        Some((_, n, t)) => (n, t),
        None => {
            sqlx::query(
                "INSERT INTO evidence_logs (log_id, tenant_id, next_seq, tree_size, kid) \
                 VALUES ($1, $2, 0, 0, $3)",
            )
            .bind(&log_id)
            .bind(tenant)
            .bind(kid)
            .execute(&mut **tx)
            .await?;
            (0, 0)
        }
    };
    debug_assert_eq!(
        next_seq, tree_size,
        "seq and leaf index are the same counter"
    );
    let mut seeded = MemoryStore::default();
    load_nodes(tx, &log_id, &peaks_for(tree_size as u64), &mut seeded).await?;
    Ok(OpenLog {
        log_id,
        size: tree_size as u64,
        store: RecordingStore::new(seeded),
    })
}

/// Load `nodes` from the database into `into`; a missing node is a
/// corrupted log and fails the transaction.
pub(crate) async fn load_nodes(
    conn: &mut sqlx::PgConnection,
    log_id: &str,
    nodes: &[(u32, u64)],
    into: &mut MemoryStore,
) -> Result<(), sqlx::Error> {
    let wanted: Vec<(u32, u64)> = nodes
        .iter()
        .copied()
        .filter(|(l, i)| into.get(*l, *i).is_none())
        .collect();
    if wanted.is_empty() {
        return Ok(());
    }
    let levels: Vec<i32> = wanted.iter().map(|(l, _)| *l as i32).collect();
    let indexes: Vec<i64> = wanted.iter().map(|(_, i)| *i as i64).collect();
    let rows: Vec<(i32, i64, String)> = sqlx::query_as(
        "SELECT n.level, n.index, n.hash FROM evidence_log_nodes n \
         JOIN UNNEST($2::int[], $3::bigint[]) AS w(level, index) \
           ON n.level = w.level AND n.index = w.index \
         WHERE n.log_id = $1",
    )
    .bind(log_id)
    .bind(&levels)
    .bind(&indexes)
    .fetch_all(&mut *conn)
    .await?;
    for (level, index, hash) in rows {
        if let Some(h) = parse_hash(&hash) {
            into.put(level as u32, index as u64, h);
        }
    }
    for (level, index) in wanted {
        if into.get(level, index).is_none() {
            return Err(sqlx::Error::Protocol(format!(
                "evidence log {log_id} is missing node ({level},{index})"
            )));
        }
    }
    Ok(())
}

/// Seal one record into the open log and insert its row and log entry.
#[allow(clippy::too_many_arguments)]
pub(crate) async fn append_record(
    tx: &mut Transaction<'_, Postgres>,
    tenant: &str,
    open: &mut OpenLog,
    item: &Item<'_>,
    signing: &SigningKey,
    kid: &str,
    iss: &str,
) -> Result<Sealed, sqlx::Error> {
    let m = item.minted;
    let mut record = m.record.clone();
    let mut record_id = m.record_id.clone();
    if let Some(chain) = chain_for(tx, tenant, m).await? {
        // Attaching a chain changes the identity: re-seal.
        if let Value::Object(map) = &mut record {
            map.insert(
                "chain".into(),
                serde_json::to_value(&chain).unwrap_or(Value::Null),
            );
            map.remove("record_id");
        }
        record_id = brutor_evidence::record::compute_record_id(&record)
            .map_err(|e| sqlx::Error::Protocol(e.to_string()))?;
        if let Value::Object(map) = &mut record {
            map.insert("record_id".into(), json!(record_id));
        }
    }
    let sub = match &m.ai_system_id {
        Some(s) => format!("ai-system:{s}"),
        None => format!("tenant:{tenant}"),
    };
    let statement = seal_record(&record, iss, &sub, signing)
        .map_err(|e| sqlx::Error::Protocol(e.to_string()))?;
    let leaf = leaf_hash(&statement);
    let idx = Log::open(&mut open.store, open.size)
        .append(leaf)
        .map_err(|e| sqlx::Error::Protocol(e.to_string()))?;
    open.size = idx + 1;
    let seq = idx as i64;
    sqlx::query(
        "INSERT INTO action_records \
         (record_id, tenant_id, log_id, seq, ai_system_id, root_task_id, proxy_log_id, verdict, kind, \
          surface, epoch, timestamp, tags, record, statement, kid) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)",
    )
    .bind(&record_id)
    .bind(tenant)
    .bind(&open.log_id)
    .bind(seq)
    .bind(&m.ai_system_id)
    .bind(&m.root_task_id)
    .bind(&m.proxy_log_id)
    .bind(&m.verdict)
    .bind(&m.kind)
    .bind(&m.surface)
    .bind(&m.epoch)
    .bind(m.timestamp)
    .bind(&m.tags)
    .bind(&record)
    .bind(&statement)
    .bind(kid)
    .execute(&mut **tx)
    .await?;
    sqlx::query(
        "INSERT INTO evidence_log_entries (log_id, seq, kind, leaf_hash, ref_id) VALUES ($1, $2, $3, $4, $5)",
    )
    .bind(&open.log_id)
    .bind(seq)
    .bind(item.entry_kind)
    .bind(hex::encode(leaf))
    .bind(item.ref_id.as_deref().unwrap_or(&record_id))
    .execute(&mut **tx)
    .await?;
    Ok(Sealed {
        row_index: m.row_index,
        record_id,
        log_id: open.log_id.clone(),
        seq,
        statement,
        record,
    })
}

async fn write_tenant(
    pool: &PgPool,
    tenant: &str,
    items: &[Item<'_>],
    signing: &SigningKey,
    force_head: bool,
) -> Result<(Vec<Sealed>, Option<IssuedHead>), sqlx::Error> {
    let mut tx: Transaction<'_, Postgres> = pool.begin().await?;
    let kid = key::kid_of(signing);
    let mut open = open_log(&mut tx, tenant, &kid).await?;
    let iss = key::issuer();
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        out.push(append_record(&mut tx, tenant, &mut open, item, signing, &kid, &iss).await?);
    }

    // Issue a signed tree head if the cadence says so (or
    // the caller forces one). Before signing, seal the heads of the
    // writer row chains that advanced for this tenant, so the head covers
    // them. The head's own stamp entry is appended inside this transaction.
    let issued = match heads::due(&mut tx, &open.log_id, open.size, force_head).await? {
        Some(d) => {
            let chain_heads = chain_head::pending(&mut tx, tenant, &open.log_id).await?;
            for m in &chain_heads {
                append_record(
                    &mut tx,
                    tenant,
                    &mut open,
                    &Item::record(m),
                    signing,
                    &kid,
                    &iss,
                )
                .await?;
            }
            let size = open.size;
            let head = heads::issue(
                &mut tx,
                &open.log_id,
                tenant,
                signing,
                &mut open.store,
                size,
                d,
            )
            .await?;
            open.size = size + 1;
            Some(head)
        }
        None => None,
    };
    if out.is_empty() && issued.is_none() {
        tx.rollback().await?;
        return Ok((out, None));
    }
    let root = Log::open(&mut open.store, open.size)
        .root()
        .map_err(|e| sqlx::Error::Protocol(e.to_string()))?;
    let new_size = open.size as i64;
    persist_nodes(&mut tx, &open.log_id, &open.store.puts).await?;
    sqlx::query(
        "UPDATE evidence_logs SET next_seq = $2, tree_size = $3, root = $4, kid = $5, \
         last_tree_head_id = COALESCE($6, last_tree_head_id), updated_at = now() \
         WHERE log_id = $1",
    )
    .bind(&open.log_id)
    .bind(new_size)
    .bind(new_size)
    .bind(hex::encode(root))
    .bind(&kid)
    .bind(issued.as_ref().map(|h| h.id.clone()))
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    if let Some(head) = &issued {
        // Registration is off the write path; a witness outage never
        // blocks a request and the retry loop picks up what fails here.
        let pool = pool.clone();
        tokio::spawn(crate::services::evidence::witness::register_head(
            pool,
            head.id.clone(),
        ));
    }
    Ok((out, issued))
}

async fn persist_nodes(
    tx: &mut Transaction<'_, Postgres>,
    log_id: &str,
    puts: &[(u32, u64, Hash)],
) -> Result<(), sqlx::Error> {
    if puts.is_empty() {
        return Ok(());
    }
    let levels: Vec<i32> = puts.iter().map(|(l, _, _)| *l as i32).collect();
    let indexes: Vec<i64> = puts.iter().map(|(_, i, _)| *i as i64).collect();
    let hashes: Vec<String> = puts.iter().map(|(_, _, h)| hex::encode(h)).collect();
    sqlx::query(
        "INSERT INTO evidence_log_nodes (log_id, level, index, hash) \
         SELECT $1, level, index, hash FROM UNNEST($2::int[], $3::bigint[], $4::text[]) AS t(level, index, hash) \
         ON CONFLICT (log_id, level, index) DO NOTHING",
    )
    .bind(log_id)
    .bind(&levels)
    .bind(&indexes)
    .bind(&hashes)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

fn parse_hash(hex_str: &str) -> Option<Hash> {
    hex::decode(hex_str).ok()?.try_into().ok()
}

/// Chains that need a database lookup: an outcome that consumed an
/// approval supersedes the `awaiting_human` record raised for it; an
/// approval decision row confirms it (non-terminal — the executed retry
/// is what closes it).
async fn chain_for(
    tx: &mut Transaction<'_, Postgres>,
    tenant: &str,
    m: &Minted,
) -> Result<Option<Chain>, sqlx::Error> {
    use crate::constants::proxy_subtype as st;
    let subtype = m
        .record
        .get("context")
        .and_then(|c| c.get("proxy_subtype"))
        .and_then(Value::as_str);
    let (approval_id, rel) = match (&m.consumed_approval_request_id, subtype) {
        (Some(id), _) => (id.clone(), relation::SUPERSEDES),
        (None, Some(st::APPROVAL_APPROVED)) => match m
            .record
            .get("disposition")
            .and_then(|d| d.get("authority"))
            .and_then(Value::as_str)
        {
            Some(id) => (id.to_owned(), relation::CONFIRMS),
            None => return Ok(None),
        },
        (None, Some(st::APPROVAL_REJECTED)) => match m
            .record
            .get("disposition")
            .and_then(|d| d.get("authority"))
            .and_then(Value::as_str)
        {
            Some(id) => (id.to_owned(), relation::SUPERSEDES),
            None => return Ok(None),
        },
        _ => return Ok(None),
    };
    let parent: Option<(String,)> = sqlx::query_as(
        "SELECT record_id FROM action_records \
         WHERE tenant_id = $1 AND verdict = 'awaiting_human' \
           AND record->'context'->>'approval_request_id' = $2 \
         ORDER BY seq ASC LIMIT 1",
    )
    .bind(tenant)
    .bind(&approval_id)
    .fetch_optional(&mut **tx)
    .await?;
    Ok(parent.map(|(p,)| Chain {
        parent_record_id: p,
        relation: rel.into(),
    }))
}

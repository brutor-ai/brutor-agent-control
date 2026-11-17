// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Signed tree heads.
//!
//! Cadence: a head is issued when at least `CADENCE_ENTRIES` records have
//! been appended since the last head, or when the first record after the
//! last head is older than `CADENCE_SECS`. Evaluated inside the same
//! transaction as the appends that made it due — an idle log is never
//! checkpointed, which is structural, not a guard: there is nothing new
//! to attest and re-signing the same root would only manufacture the
//! appearance of activity.
//!
//! The head is a Signed Statement over `{log_id, tree_size, root,
//! prev_size, prev_root, issued_at, consistency_proof}` and is then
//! appended to its own log as a `tree_head` entry, so head N is covered
//! by head N+1's root: a byte flipped in a stored head breaks the next
//! root. Witness registration happens after commit.

use brutor_cose::merkle::{Hash, leaf_hash};
use brutor_ctlog::{
    Log, MemoryStore, NodeStore, RecordingStore, TreeHead, consistency_proof_nodes,
};
use sqlx::{Postgres, Transaction};

pub const CADENCE_ENTRIES: i64 = 100;
pub const CADENCE_SECS: i64 = 15 * 60;

/// The tree-head cadence `env` configures, as `(entries, seconds)`
/// (`BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES`,
/// `BRUTOR_EVIDENCE_HEAD_CADENCE_SECS`). 0 and anything that is not a number
/// are refused: the gateway does not start with them (`config::check`).
pub fn cadence_from(
    env: &crate::env_registry::Env,
) -> Result<(i64, i64), crate::env_registry::EnvError> {
    use crate::env_vars::{self, positive};
    let entries = positive(env, &env_vars::BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES);
    let secs = positive(env, &env_vars::BRUTOR_EVIDENCE_HEAD_CADENCE_SECS);
    crate::env_vars::all_of([
        entries.as_ref().map(drop).map_err(Clone::clone),
        secs.as_ref().map(drop).map_err(Clone::clone),
    ])?;
    Ok((entries?, secs?))
}

fn cadence() -> (i64, i64) {
    cadence_from(&crate::env_vars::process()).unwrap_or((CADENCE_ENTRIES, CADENCE_SECS))
}

pub struct IssuedHead {
    pub id: String,
    pub tree_size: u64,
    pub root: Hash,
    /// The signed tree head (COSE_Sign1 bytes).
    pub statement: Vec<u8>,
}

/// A head is due: what the previous head committed to.
#[derive(Debug, Clone)]
pub struct DueHead {
    pub prev_size: u64,
    pub prev_root: Option<Hash>,
}

/// Is a head due for a log advanced to `size`? `force` skips the cadence
/// but never re-signs a covered log: with nothing appended since the last
/// head (its own stamp excepted) the answer is `None`.
pub async fn due(
    tx: &mut Transaction<'_, Postgres>,
    log_id: &str,
    size: u64,
    force: bool,
) -> Result<Option<DueHead>, sqlx::Error> {
    let last: Option<(i64, String)> = sqlx::query_as(
        "SELECT tree_size, root FROM evidence_tree_heads \
         WHERE log_id = $1 ORDER BY tree_size DESC LIMIT 1",
    )
    .bind(log_id)
    .fetch_optional(&mut **tx)
    .await?;
    let (prev_size, prev_root): (u64, Option<Hash>) = match &last {
        Some((ts, root)) => (
            *ts as u64,
            hex::decode(root).ok().and_then(|b| b.try_into().ok()),
        ),
        None => (0, None),
    };
    // Entries after the previous head include that head's own stamp.
    let first_new_seq = if last.is_some() { prev_size + 1 } else { 0 };
    if size <= first_new_seq {
        return Ok(None);
    }
    let new_records = (size - first_new_seq) as i64;
    let (cad_entries, cad_secs) = cadence();
    let mut is_due = force || new_records >= cad_entries;
    if !is_due {
        let first: Option<(chrono::DateTime<chrono::Utc>,)> = sqlx::query_as(
            "SELECT created_at FROM evidence_log_entries WHERE log_id = $1 AND seq = $2",
        )
        .bind(log_id)
        .bind(first_new_seq as i64)
        .fetch_optional(&mut **tx)
        .await?;
        if let Some((at,)) = first {
            is_due = (chrono::Utc::now() - at).num_seconds() >= cad_secs;
        }
    }
    Ok(is_due.then_some(DueHead {
        prev_size,
        prev_root,
    }))
}

/// Issue a head if one is due. Runs inside the caller's transaction with
/// the log already advanced to `size` in `store`; on issue it appends the
/// stamp entry (so the store's size grows by one) and returns the head.
#[allow(clippy::too_many_arguments)]
pub async fn maybe_issue(
    tx: &mut Transaction<'_, Postgres>,
    log_id: &str,
    tenant: &str,
    signing: &ed25519_dalek::SigningKey,
    store: &mut RecordingStore<MemoryStore>,
    size: u64,
    force: bool,
) -> Result<Option<IssuedHead>, sqlx::Error> {
    match due(tx, log_id, size, force).await? {
        Some(d) => Ok(Some(
            issue(tx, log_id, tenant, signing, store, size, d).await?,
        )),
        None => Ok(None),
    }
}

/// Sign and persist a head over the first `size` leaves, then stamp it into
/// its own log as leaf `size`.
pub async fn issue(
    tx: &mut Transaction<'_, Postgres>,
    log_id: &str,
    tenant: &str,
    signing: &ed25519_dalek::SigningKey,
    store: &mut RecordingStore<MemoryStore>,
    size: u64,
    d: DueHead,
) -> Result<IssuedHead, sqlx::Error> {
    let DueHead {
        prev_size,
        prev_root,
    } = d;
    // Preload the nodes the consistency proof reads that are not peaks.
    if prev_size > 0 {
        for (level, index) in consistency_proof_nodes(prev_size, size) {
            if store.get(level, index).is_some() {
                continue;
            }
            let h: Option<(String,)> = sqlx::query_as(
                "SELECT hash FROM evidence_log_nodes WHERE log_id = $1 AND level = $2 AND index = $3",
            )
            .bind(log_id)
            .bind(level as i32)
            .bind(index as i64)
            .fetch_optional(&mut **tx)
            .await?;
            match h
                .and_then(|(x,)| hex::decode(x).ok())
                .and_then(|b| b.try_into().ok())
            {
                Some(hash) => store.inner.put(level, index, hash),
                None => {
                    return Err(sqlx::Error::Protocol(format!(
                        "evidence log {log_id} missing node ({level},{index}) for consistency proof {prev_size}->{size}"
                    )));
                }
            }
        }
    }
    let mut log = Log::open(store, size);
    let root = log
        .root()
        .map_err(|e| sqlx::Error::Protocol(e.to_string()))?;
    let proof = if prev_size > 0 {
        log.consistency_proof(prev_size, size)
            .map_err(|e| sqlx::Error::Protocol(e.to_string()))?
    } else {
        vec![]
    };
    let issued_at = chrono::Utc::now();
    let head = TreeHead {
        log_id: log_id.to_string(),
        tree_size: size,
        root,
        prev_size,
        prev_root,
        issued_at: issued_at.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
        consistency_proof: proof,
    };
    let statement = head
        .sign(signing)
        .map_err(|e| sqlx::Error::Protocol(e.to_string()))?;
    let id = format!("thd-{}", ulid::Ulid::new());
    sqlx::query(
        "INSERT INTO evidence_tree_heads \
         (id, log_id, tenant_id, tree_size, root, prev_size, prev_root, statement, issued_at, grade) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'standalone')",
    )
    .bind(&id)
    .bind(log_id)
    .bind(tenant)
    .bind(size as i64)
    .bind(hex::encode(root))
    .bind(prev_size as i64)
    .bind(prev_root.map(hex::encode))
    .bind(&statement)
    .bind(issued_at)
    .execute(&mut **tx)
    .await?;
    // Stamp the head into its own log.
    let leaf = leaf_hash(&statement);
    let stamp_seq = log
        .append(leaf)
        .map_err(|e| sqlx::Error::Protocol(e.to_string()))?;
    sqlx::query(
        "INSERT INTO evidence_log_entries (log_id, seq, kind, leaf_hash, ref_id) VALUES ($1, $2, 'tree_head', $3, $4)",
    )
    .bind(log_id)
    .bind(stamp_seq as i64)
    .bind(hex::encode(leaf))
    .bind(&id)
    .execute(&mut **tx)
    .await?;
    sqlx::query("UPDATE evidence_tree_heads SET stamp_seq = $2 WHERE id = $1")
        .bind(&id)
        .bind(stamp_seq as i64)
        .execute(&mut **tx)
        .await?;
    Ok(IssuedHead {
        id,
        tree_size: size,
        root,
        statement,
    })
}

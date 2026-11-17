// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The row chain joins the same trust.
//!
//! The audit chain is per *writer* (one per replica process), signed
//! at each flush into `audit_chain_checkpoints`. Its checkpoints are
//! signed with the audit key, which no relying party of the evidence log
//! pins. So at every tree-head issue for a tenant, just before the head is
//! signed, each writer chain that carried this tenant's rows and whose
//! checkpoint has advanced since the last one sealed here becomes an `fyi`
//! record `ai.brutor.audit-chain-head` in the tenant log:
//!
//! * `context` — `{record_type, surface: "audit_chain", chain_id, seq_end}`
//!   (the chain id is a replica/ULID handle, never an identity);
//! * `references` — the chain head hash (`ai.brutor.audit-chain-head`) and
//!   SHA-256 of the checkpoint signature bytes
//!   (`ai.brutor.audit-checkpoint-signature`).
//!
//! The head that follows covers those records, so a relying party who
//! trusts the tenant log (and its witnesses) now also holds a witnessed
//! commitment to the row chain up to `seq_end` — rewriting `proxy_logs`
//! would have to rewrite the witnessed log too.
//!
//! Bounded work: only chains that appear on this tenant's rows since the
//! previous head (with a 15-minute overlap for checkpoints written after
//! their rows), at most [`MAX_CHAINS_PER_HEAD`] per head, deduplicated by
//! `action_id` so a chain head is sealed into a tenant log once.

use crate::services::evidence::mint::Minted;
use brutor_evidence::record::{Disposition, RecordBuilder, Reference, approver, decision, kind};
use serde_json::{Map, json};
use sha2::{Digest, Sha256};
use sqlx::{Postgres, Transaction};

pub const RECORD_TYPE: &str = "ai.brutor.audit-chain-head";
pub const SURFACE: &str = "audit_chain";
pub const MAX_CHAINS_PER_HEAD: i64 = 32;
/// With no previous head, look this far back for chain activity.
const FIRST_HEAD_LOOKBACK: &str = "1 hour";
/// Checkpoints are written after their rows; overlap the window by this.
const OVERLAP: &str = "15 minutes";

/// The chain-head records due for `tenant`'s log, built but not appended.
/// Runs inside the head-issuing transaction; any failure is logged and
/// yields no records — the row chain is still checkpointed, it just isn't
/// sealed into this head.
pub async fn pending(
    tx: &mut Transaction<'_, Postgres>,
    tenant: &str,
    log_id: &str,
) -> Result<Vec<Minted>, sqlx::Error> {
    // Savepoint so a failure here (a schema without the chain tables) never
    // aborts the caller's transaction.
    sqlx::query("SAVEPOINT evidence_chain_head")
        .execute(&mut **tx)
        .await?;
    match build(tx, tenant, log_id).await {
        Ok(v) => {
            sqlx::query("RELEASE SAVEPOINT evidence_chain_head")
                .execute(&mut **tx)
                .await?;
            Ok(v)
        }
        Err(e) => {
            tracing::debug!(
                target = "brutor::evidence",
                tenant_id = %tenant,
                error = %e,
                "audit-chain head records not sealed with this tree head",
            );
            sqlx::query("ROLLBACK TO SAVEPOINT evidence_chain_head")
                .execute(&mut **tx)
                .await?;
            Ok(vec![])
        }
    }
}

async fn build(
    tx: &mut Transaction<'_, Postgres>,
    tenant: &str,
    log_id: &str,
) -> Result<Vec<Minted>, sqlx::Error> {
    // The newest checkpoint per chain that covers this tenant's newest row
    // on that chain, for chains active on the tenant since the last head.
    let rows: Vec<(String, i64, String, String)> = sqlx::query_as(&format!(
        "WITH since AS ( \
           SELECT COALESCE(MAX(issued_at) - interval '{OVERLAP}', now() - interval '{FIRST_HEAD_LOOKBACK}') AS t \
           FROM evidence_tree_heads WHERE log_id = $2 \
         ), chains AS ( \
           SELECT p.chain_id, MAX(p.chain_seq) AS seq \
           FROM proxy_logs p, since \
           WHERE p.tenant_id = $1 AND p.timestamp >= since.t AND p.chain_id IS NOT NULL \
           GROUP BY p.chain_id \
           ORDER BY MAX(p.timestamp) DESC LIMIT {MAX_CHAINS_PER_HEAD} \
         ) \
         SELECT c.chain_id, k.seq_end, k.head_hash, k.signature FROM chains c \
         CROSS JOIN LATERAL ( \
           SELECT seq_end, head_hash, signature FROM audit_chain_checkpoints a \
           WHERE a.chain_id = c.chain_id AND a.seq_end >= c.seq \
           ORDER BY a.seq_end DESC LIMIT 1 \
         ) k \
         ORDER BY c.chain_id"
    ))
    .bind(tenant)
    .bind(log_id)
    .fetch_all(&mut **tx)
    .await?;
    if rows.is_empty() {
        return Ok(vec![]);
    }
    let action_ids: Vec<String> = rows.iter().map(|(c, s, _, _)| action_id(c, *s)).collect();
    let existing: Vec<(String,)> = sqlx::query_as(
        "SELECT record->>'action_id' FROM action_records \
         WHERE tenant_id = $1 AND surface = $2 AND record->>'action_id' = ANY($3)",
    )
    .bind(tenant)
    .bind(SURFACE)
    .bind(&action_ids)
    .fetch_all(&mut **tx)
    .await?;
    let now = chrono::Utc::now();
    let mut out = Vec::new();
    for (chain_id, seq_end, head_hash, signature) in rows {
        let aid = action_id(&chain_id, seq_end);
        if existing.iter().any(|(e,)| *e == aid) {
            continue;
        }
        match record_for(tenant, &chain_id, seq_end, &head_hash, &signature, now) {
            Ok(m) => out.push(m),
            Err(e) => tracing::warn!(
                target = "brutor::evidence",
                chain_id = %chain_id,
                error = %e,
                "audit-chain head record could not be built",
            ),
        }
    }
    Ok(out)
}

fn action_id(chain_id: &str, seq_end: i64) -> String {
    format!("{RECORD_TYPE}/{chain_id}/{seq_end}")
}

/// Build the `fyi` record for one checkpoint.
pub fn record_for(
    tenant: &str,
    chain_id: &str,
    seq_end: i64,
    head_hash: &str,
    signature_hex: &str,
    at: chrono::DateTime<chrono::Utc>,
) -> Result<Minted, brutor_evidence::RecordError> {
    let ts = at.to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
    let mut b = RecordBuilder::new(action_id(chain_id, seq_end), tenant, ts);
    b.kind = kind::FYI.into();
    b.verdict = brutor_evidence::record::verdict::RECORDED.into();
    b.disposition = Some(Disposition {
        decision: decision::ACCEPT.into(),
        approver: approver::POLICY.into(),
        human_disposed: false,
        authority: None,
        reason_digest: None,
    });
    let mut ctx = Map::new();
    ctx.insert("record_type".into(), json!(RECORD_TYPE));
    ctx.insert("surface".into(), json!(SURFACE));
    ctx.insert("chain_id".into(), json!(chain_id));
    ctx.insert("seq_end".into(), json!(seq_end));
    b.context = ctx;
    let sig_bytes =
        hex::decode(signature_hex).unwrap_or_else(|_| signature_hex.as_bytes().to_vec());
    b.references = Some(vec![
        Reference {
            kind: "ai.brutor.audit-chain-head".into(),
            digest_alg: "SHA-256".into(),
            digest: head_hash.to_ascii_lowercase(),
            purpose: Some("row_chain_head".into()),
        },
        Reference {
            kind: "ai.brutor.audit-checkpoint-signature".into(),
            digest_alg: "SHA-256".into(),
            digest: hex::encode(Sha256::digest(&sig_bytes)),
            purpose: Some("checkpoint_signature".into()),
        },
    ]);
    let (record, record_id) = b.seal()?;
    Ok(Minted {
        row_index: 0,
        tenant_id: tenant.to_string(),
        record,
        record_id,
        ai_system_id: None,
        root_task_id: None,
        proxy_log_id: None,
        verdict: brutor_evidence::record::verdict::RECORDED.into(),
        kind: kind::FYI.into(),
        surface: SURFACE.into(),
        epoch: None,
        timestamp: at,
        tags: None,
        approval_request_id: None,
        consumed_approval_request_id: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chain_head_record_is_valid_and_carries_digests_only() {
        let m = record_for(
            "t1",
            "replica-a-01J",
            41,
            &"ab".repeat(32),
            &"cd".repeat(64),
            chrono::Utc::now(),
        )
        .unwrap();
        let v = brutor_evidence::verify(&m.record, None);
        assert!(v.ok, "{:?}", v.findings);
        assert_eq!(m.record["context"]["record_type"], RECORD_TYPE);
        assert_eq!(m.record["context"]["seq_end"], 41);
        assert_eq!(m.record["kind"], "fyi");
        let refs = m.record["references"].as_array().unwrap();
        assert_eq!(refs[0]["digest"], "ab".repeat(32));
        // The signature enters as a digest, not in clear.
        assert!(!m.record.to_string().contains(&"cd".repeat(64)));
    }
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Approval expiry records (`ai.brutor.approval-expired`).
//!
//! An approval nobody actioned is a verdict too: `expired`,
//! deferred / policy / false, chained `supersedes` → the `awaiting_human`
//! record raised when the call was enqueued (found through
//! `record->'context'->>'approval_request_id'`). Without it the may/did
//! chain of an unanswered approval stays open forever and "every verdict
//! sealed" cannot hold.
//!
//! Sealed by whichever path moves a request to `expired` — the sweeper,
//! the lazy check on an approve that arrived too late, and a run abort that
//! expires the run's pending approvals.

use crate::services::evidence::{key, mint::Minted, store};
use brutor_evidence::record::{
    self, Chain, Disposition, RecordBuilder, RecordError, System, approver, decision, kind,
    relation, verdict,
};
use serde_json::{Map, Value, json};
use sqlx::PgPool;

pub const RECORD_TYPE: &str = "ai.brutor.approval-expired";

/// The `awaiting_human` record an expiry supersedes, as the store keeps it.
#[derive(Debug, Clone, Default)]
pub struct AwaitingRecord {
    pub record_id: String,
    pub ai_system_id: Option<String>,
    pub root_task_id: Option<String>,
    pub surface: Option<String>,
    pub epoch: Option<String>,
    /// The `system` member of the parent record, carried over verbatim.
    pub system: Option<Value>,
}

/// Build the `expired` record. Pure; `parent = None` when no
/// `awaiting_human` record exists for the request (evidence was off when it
/// was raised) — the expiry is still a verdict worth sealing.
pub fn build_expired(
    tenant_id: &str,
    approval_request_id: &str,
    expired_at: chrono::DateTime<chrono::Utc>,
    parent: Option<&AwaitingRecord>,
) -> Result<Minted, RecordError> {
    let mut b = RecordBuilder::new(
        format!("approval/{approval_request_id}/expired"),
        tenant_id.to_string(),
        expired_at.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
    );
    b.kind = kind::DECIDE.into();
    b.verdict = verdict::EXPIRED.into();
    b.system = parent
        .and_then(|p| p.system.as_ref())
        .and_then(|s| serde_json::from_value::<System>(s.clone()).ok());
    b.epoch = parent.and_then(|p| p.epoch.clone());
    b.disposition = Some(Disposition {
        decision: decision::DEFERRED.into(),
        approver: approver::POLICY.into(),
        human_disposed: false,
        authority: Some(approval_request_id.to_string()),
        reason_digest: None,
    });
    let mut ctx = Map::new();
    ctx.insert("record_type".into(), json!(RECORD_TYPE));
    ctx.insert("approval_request_id".into(), json!(approval_request_id));
    if let Some(s) = parent.and_then(|p| p.surface.as_ref()) {
        ctx.insert("surface".into(), json!(s));
    }
    if let Some(r) = parent.and_then(|p| p.root_task_id.as_ref()) {
        ctx.insert("root_task_id".into(), json!(r));
    }
    b.context = ctx;
    if let Some(p) = parent.filter(|p| record::is_hex64(&p.record_id)) {
        b.chain = Some(Chain {
            parent_record_id: p.record_id.clone(),
            relation: relation::SUPERSEDES.into(),
        });
    }
    let (rec, record_id) = b.seal()?;
    Ok(Minted {
        row_index: 0,
        tenant_id: tenant_id.to_string(),
        record: rec,
        record_id,
        ai_system_id: parent.and_then(|p| p.ai_system_id.clone()),
        root_task_id: parent.and_then(|p| p.root_task_id.clone()),
        proxy_log_id: None,
        verdict: verdict::EXPIRED.into(),
        kind: kind::DECIDE.into(),
        surface: parent
            .and_then(|p| p.surface.clone())
            .unwrap_or_else(|| "approval".into()),
        epoch: parent.and_then(|p| p.epoch.clone()),
        timestamp: expired_at,
        tags: None,
        // Deliberately not set: `store`'s approval chain lookup keys on these
        // and this record carries its own chain.
        approval_request_id: None,
        consumed_approval_request_id: None,
    })
}

type AwaitingRow = (
    String,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Value,
);

async fn awaiting_for(pool: &PgPool, tenant_id: &str, approval_id: &str) -> Option<AwaitingRecord> {
    let row: Option<AwaitingRow> = sqlx::query_as(
        "SELECT record_id, ai_system_id, root_task_id, surface, epoch, record FROM action_records \
         WHERE tenant_id = $1 AND verdict = 'awaiting_human' \
           AND record->'context'->>'approval_request_id' = $2 \
         ORDER BY seq ASC LIMIT 1",
    )
    .bind(tenant_id)
    .bind(approval_id)
    .fetch_optional(pool)
    .await
    .map_err(|e| {
        tracing::error!(
            target = "brutor::evidence",
            error = %e,
            "awaiting_human lookup for an expired approval failed",
        )
    })
    .ok()
    .flatten();
    row.map(
        |(record_id, ai_system_id, root_task_id, surface, epoch, rec)| AwaitingRecord {
            record_id,
            ai_system_id,
            root_task_id,
            surface,
            epoch,
            system: rec.get("system").filter(|s| s.is_object()).cloned(),
        },
    )
}

/// Seal one `expired` record per `(approval_request_id, tenant_id)`.
/// Best-effort: an approval's status has already moved; a record that
/// cannot be sealed is logged and counted, never an error to the sweeper.
pub async fn seal_expired(pool: &PgPool, expired: &[(String, String)]) {
    if !key::is_enabled() || expired.is_empty() {
        return;
    }
    let now = chrono::Utc::now();
    for (approval_id, tenant_id) in expired {
        let parent = awaiting_for(pool, tenant_id, approval_id).await;
        let minted = match build_expired(tenant_id, approval_id, now, parent.as_ref()) {
            Ok(m) => m,
            Err(e) => {
                tracing::error!(target = "brutor::evidence", error = %e, "expired record build failed");
                continue;
            }
        };
        if let Err(e) = store::seal(pool, tenant_id, std::slice::from_ref(&minted)).await {
            tracing::warn!(
                target = "brutor::evidence",
                tenant_id = %tenant_id,
                approval_request_id = %approval_id,
                error = %e,
                "approval-expired record not sealed",
            );
            crate::services::evidence::metrics::capture_gap(
                crate::services::evidence::metrics::gap_reason::SEAL_FAILED,
                1,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parent() -> AwaitingRecord {
        AwaitingRecord {
            record_id: "f".repeat(64),
            ai_system_id: Some("sys-1".into()),
            root_task_id: Some("run-1".into()),
            surface: Some("mcp".into()),
            epoch: Some(format!("contract:{}", "d".repeat(64))),
            system: Some(
                json!({"ai_system_id": "sys-1", "contract_version": 3, "contract_hash": "d".repeat(64)}),
            ),
        }
    }

    #[test]
    fn expired_is_deferred_by_policy_and_supersedes_the_awaiting_record() {
        let m = build_expired("tenant-a", "apr-9", chrono::Utc::now(), Some(&parent())).unwrap();
        let r = &m.record;
        assert_eq!(m.verdict, "expired");
        assert_eq!(r["verdict"], "expired");
        assert_eq!(r["disposition"]["decision"], "deferred");
        assert_eq!(r["disposition"]["approver"], "policy");
        assert_eq!(r["disposition"]["human_disposed"], false);
        assert_eq!(r["context"]["record_type"], RECORD_TYPE);
        assert_eq!(r["context"]["approval_request_id"], "apr-9");
        assert_eq!(r["chain"]["relation"], "supersedes");
        assert_eq!(r["chain"]["parent_record_id"], "f".repeat(64));
        assert_eq!(r["system"]["ai_system_id"], "sys-1");
        assert!(r.get("effect").is_none());
        assert_eq!(m.ai_system_id.as_deref(), Some("sys-1"));
        assert!(m.approval_request_id.is_none() && m.consumed_approval_request_id.is_none());
        let v = brutor_evidence::verify(r, None);
        assert!(v.ok, "{:?}", v.findings);
        assert_eq!(v.effect_mode, Some("not_applicable"));
    }

    #[test]
    fn expiry_without_an_awaiting_record_still_seals_unchained() {
        let m = build_expired("tenant-a", "apr-9", chrono::Utc::now(), None).unwrap();
        assert!(m.record.get("chain").is_none());
        assert_eq!(m.record["system"], "ungoverned");
        assert_eq!(m.surface, "approval");
        assert!(brutor_evidence::verify(&m.record, None).ok);
    }
}

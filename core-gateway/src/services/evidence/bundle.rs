// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `POST /v1/evidence/bundles` — assembling an evidence bundle from the tenant log.
//!
//! The pure build/verify lives in `brutor_evidence::bundle`; this module
//! does the database side:
//!
//! 1. **Select** — an explicit id list (`producer-selected`), a run
//!    (`root_task_id`) or an AI System + window (both `contiguous`). Over
//!    [`MAX_BUNDLE_RECORDS`] ⇒ 413, never a silent truncation.
//! 2. **Close** — follow `chain.parent_record_id` up to `closure_depth`
//!    hops. A parent that is not in the tenant's log, or lies beyond the
//!    depth, is listed in `missing[]` — never dropped.
//! 3. **Cover** — if the newest selected record is not under a signed
//!    head, force one ([`store::force_head`]) so every proof is against a
//!    signed head.
//! 4. **Prove** — for each record, an inclusion proof against the newest
//!    head; included heads = the first head covering each record plus the
//!    newest, with consistency proofs between consecutive ones, so the
//!    bundle shows *when* each record was first committed and that the
//!    history is append-only. Only the nodes those proofs read are loaded.
//! 5. **Seal** — the bundle digest as an `ai.brutor.bundle` record; for a
//!    named audience also an `ai.brutor.disclosure` record whose log entry
//!    is kind `disclosure`, plus the `disclosure_records` row. Both land in
//!    one transaction.
//! 6. **Persist** `evidence_bundles`.

use crate::services::evidence::key;
use crate::services::evidence::mint::Minted;
use crate::services::evidence::store::{self, Item, SealError};
use base64::Engine as _;
use brutor_ctlog::{Log, MemoryStore, consistency_proof_nodes, inclusion_path_nodes};
use brutor_evidence::bundle::{
    self as eb, BundleHead, BundleInput, BundleReceipt, BundleRecord, ConsistencyProof,
    MAX_BUNDLE_RECORDS, RecordProof, VerifyOptions, selection,
};
use brutor_evidence::record::{
    Disposition, RecordBuilder, Reference, approver, decision, is_hex64, kind, verdict,
};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use sqlx::PgPool;
use std::collections::{BTreeSet, HashSet};

/// Serialized bundle size cap.
pub const MAX_BUNDLE_BYTES: usize = 50 * 1024 * 1024;
pub const MAX_CLOSURE_DEPTH: u32 = 8;
pub const BUNDLE_RECORD_TYPE: &str = "ai.brutor.bundle";
pub const DISCLOSURE_RECORD_TYPE: &str = "ai.brutor.disclosure";

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BundleRequest {
    pub tenant_id: String,
    pub selector: Selector,
    #[serde(default)]
    pub audience: Option<String>,
    #[serde(default = "default_payloads")]
    pub payloads: String,
    #[serde(default)]
    pub framework_id: Option<String>,
    #[serde(default)]
    pub catalog_version: Option<String>,
    #[serde(default)]
    pub obligation_key: Option<String>,
    #[serde(default)]
    pub extensions: Option<Map<String, Value>>,
    #[serde(default = "default_depth")]
    pub closure_depth: u32,
    #[serde(default)]
    pub requested_by: Option<String>,
}

fn default_payloads() -> String {
    "none".into()
}
fn default_depth() -> u32 {
    1
}

#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
pub enum Selector {
    RecordIds {
        record_ids: Vec<String>,
    },
    Run {
        root_task_id: String,
    },
    Window {
        ai_system_id: String,
        from: chrono::DateTime<chrono::Utc>,
        to: chrono::DateTime<chrono::Utc>,
    },
}

impl Selector {
    fn selection(&self) -> &'static str {
        match self {
            Self::RecordIds { .. } => selection::PRODUCER_SELECTED,
            _ => selection::CONTIGUOUS,
        }
    }
    fn to_json(&self) -> Value {
        match self {
            Self::RecordIds { record_ids } => json!({"record_ids": record_ids}),
            Self::Run { root_task_id } => json!({"root_task_id": root_task_id}),
            Self::Window {
                ai_system_id,
                from,
                to,
            } => json!({
                "ai_system_id": ai_system_id,
                "from": from.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
                "to": to.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
            }),
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum BundleApiError {
    /// 422
    #[error("{0}")]
    Invalid(String),
    /// 409 — `payloads: "all"` cannot be met: bodies never enter records.
    #[error("payloads_unavailable")]
    PayloadsUnavailable,
    /// 413
    #[error("{0}")]
    TooLarge(String),
    /// 404 — nothing matched the selector.
    #[error("no records match the selector")]
    Empty,
    #[error(transparent)]
    Seal(#[from] SealError),
    #[error(transparent)]
    Db(#[from] sqlx::Error),
    /// 500 — the assembled bundle failed its own verification.
    #[error("{0}")]
    Internal(String),
}

#[derive(Debug, Clone)]
pub struct BuiltBundle {
    pub bundle_id: String,
    pub bundle_digest: String,
    pub record_id: String,
    pub disclosure_id: Option<String>,
    pub bundle: Value,
    pub persisted: bool,
}

#[derive(Debug, Clone, sqlx::FromRow)]
struct Row {
    record_id: String,
    seq: i64,
    record: Value,
    statement: Vec<u8>,
}

const ROW_COLS: &str = "record_id, seq, record, statement";

fn parent_of(r: &Value) -> Option<String> {
    r.get("chain")
        .and_then(|c| c.get("parent_record_id"))
        .and_then(Value::as_str)
        .filter(|p| is_hex64(p))
        .map(str::to_owned)
}

fn check(req: &BundleRequest) -> Result<(), BundleApiError> {
    if req.tenant_id.trim().is_empty() {
        return Err(BundleApiError::Invalid("tenant_id is required".into()));
    }
    match req.payloads.as_str() {
        "none" => {}
        "all" => return Err(BundleApiError::PayloadsUnavailable),
        other => {
            return Err(BundleApiError::Invalid(format!(
                "payloads must be \"none\" or \"all\", got {other:?}"
            )));
        }
    }
    if req.closure_depth > MAX_CLOSURE_DEPTH {
        return Err(BundleApiError::Invalid(format!(
            "closure_depth is at most {MAX_CLOSURE_DEPTH}"
        )));
    }
    if let Some(a) = &req.audience
        && (a.trim().is_empty() || a.len() > 255)
    {
        return Err(BundleApiError::Invalid(
            "audience must be a short label".into(),
        ));
    }
    match &req.selector {
        Selector::RecordIds { record_ids } => {
            if record_ids.is_empty() {
                return Err(BundleApiError::Invalid("record_ids is empty".into()));
            }
            if record_ids.len() > MAX_BUNDLE_RECORDS {
                return Err(BundleApiError::TooLarge(format!(
                    "at most {MAX_BUNDLE_RECORDS} records per bundle"
                )));
            }
            if let Some(bad) = record_ids.iter().find(|r| !is_hex64(r)) {
                return Err(BundleApiError::Invalid(format!(
                    "record id {bad:?} is not 64 lowercase hex"
                )));
            }
        }
        Selector::Run { root_task_id } if root_task_id.is_empty() => {
            return Err(BundleApiError::Invalid("root_task_id is empty".into()));
        }
        Selector::Window { from, to, .. } if from >= to => {
            return Err(BundleApiError::Invalid("from must be before to".into()));
        }
        _ => {}
    }
    Ok(())
}

async fn select(pool: &PgPool, tenant: &str, sel: &Selector) -> Result<Vec<Row>, BundleApiError> {
    let cap = MAX_BUNDLE_RECORDS as i64 + 1;
    let rows: Vec<Row> = match sel {
        Selector::RecordIds { record_ids } => {
            sqlx::query_as(&format!(
                "SELECT {ROW_COLS} FROM action_records WHERE tenant_id = $1 AND record_id = ANY($2) ORDER BY seq"
            ))
            .bind(tenant)
            .bind(record_ids)
            .fetch_all(pool)
            .await?
        }
        Selector::Run { root_task_id } => {
            sqlx::query_as(&format!(
                "SELECT {ROW_COLS} FROM action_records WHERE tenant_id = $1 AND root_task_id = $2 \
                 ORDER BY seq LIMIT $3"
            ))
            .bind(tenant)
            .bind(root_task_id)
            .bind(cap)
            .fetch_all(pool)
            .await?
        }
        Selector::Window {
            ai_system_id,
            from,
            to,
        } => {
            sqlx::query_as(&format!(
                "SELECT {ROW_COLS} FROM action_records WHERE tenant_id = $1 AND ai_system_id = $2 \
                 AND timestamp >= $3 AND timestamp < $4 ORDER BY seq LIMIT $5"
            ))
            .bind(tenant)
            .bind(ai_system_id)
            .bind(from)
            .bind(to)
            .bind(cap)
            .fetch_all(pool)
            .await?
        }
    };
    if rows.len() > MAX_BUNDLE_RECORDS {
        return Err(BundleApiError::TooLarge(format!(
            "the selector matches more than {MAX_BUNDLE_RECORDS} records; narrow the window"
        )));
    }
    Ok(rows)
}

/// Follow chain parents `depth` hops. Returns the added rows and the
/// parents that could not be included (absent, or beyond the depth).
async fn close(
    pool: &PgPool,
    tenant: &str,
    rows: &[Row],
    depth: u32,
) -> Result<(Vec<Row>, BTreeSet<String>), BundleApiError> {
    let mut have: HashSet<String> = rows.iter().map(|r| r.record_id.clone()).collect();
    let mut frontier: Vec<String> = rows
        .iter()
        .filter_map(|r| parent_of(&r.record))
        .filter(|p| !have.contains(p))
        .collect();
    let mut added = Vec::new();
    let mut missing = BTreeSet::new();
    for _ in 0..depth {
        frontier.sort();
        frontier.dedup();
        if frontier.is_empty() {
            break;
        }
        let found: Vec<Row> = sqlx::query_as(&format!(
            "SELECT {ROW_COLS} FROM action_records WHERE tenant_id = $1 AND record_id = ANY($2)"
        ))
        .bind(tenant)
        .bind(&frontier)
        .fetch_all(pool)
        .await?;
        let found_ids: HashSet<&str> = found.iter().map(|r| r.record_id.as_str()).collect();
        for p in &frontier {
            if !found_ids.contains(p.as_str()) {
                missing.insert(p.clone());
            }
        }
        let mut next = Vec::new();
        for r in found {
            if have.insert(r.record_id.clone()) {
                if let Some(p) = parent_of(&r.record)
                    && !have.contains(&p)
                {
                    next.push(p);
                }
                added.push(r);
            }
        }
        if have.len() > MAX_BUNDLE_RECORDS {
            return Err(BundleApiError::TooLarge(format!(
                "the closure exceeds {MAX_BUNDLE_RECORDS} records; lower closure_depth"
            )));
        }
        frontier = next;
    }
    // Parents beyond the depth are declared, not silently cut.
    for p in frontier {
        if !have.contains(&p) {
            missing.insert(p);
        }
    }
    Ok((added, missing))
}

#[derive(Debug, sqlx::FromRow)]
struct HeadRow {
    id: String,
    tree_size: i64,
    statement: Vec<u8>,
    transparent_statement: Option<Vec<u8>>,
}

fn enc(b: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(b)
}

/// Build, seal and persist a bundle.
pub async fn build(pool: &PgPool, req: &BundleRequest) -> Result<BuiltBundle, BundleApiError> {
    check(req)?;
    let signing = key::signing_key_for(pool, &req.tenant_id)
        .await
        .map_err(SealError::from)?
        .ok_or(SealError::Disabled)?;
    let tenant = req.tenant_id.as_str();
    let log_id = key::log_id_of(pool, tenant)
        .await
        .map_err(SealError::from)?;

    let mut rows = select(pool, tenant, &req.selector).await?;
    if rows.is_empty() {
        return Err(BundleApiError::Empty);
    }
    let root_record_id = match &req.selector {
        Selector::RecordIds { record_ids } if record_ids.len() == 1 => Some(record_ids[0].clone()),
        _ => None,
    };
    let (added, mut missing) = close(pool, tenant, &rows, req.closure_depth).await?;
    rows.extend(added);
    if let Selector::RecordIds { record_ids } = &req.selector {
        // Asked for but not in this tenant's log: listed, never dropped.
        let have: HashSet<&str> = rows.iter().map(|r| r.record_id.as_str()).collect();
        for id in record_ids {
            if !have.contains(id.as_str()) {
                missing.insert(id.clone());
            }
        }
    }
    rows.sort_by_key(|r| r.seq);
    rows.dedup_by(|a, b| a.record_id == b.record_id);
    let max_seq = rows.last().map(|r| r.seq).unwrap_or(0);

    // Cover: every proof is against a signed head.
    let mut newest = sqlx::query_as::<_, HeadRow>(
        "SELECT id, tree_size, statement, transparent_statement FROM evidence_tree_heads \
         WHERE log_id = $1 ORDER BY tree_size DESC LIMIT 1",
    )
    .bind(&log_id)
    .fetch_optional(pool)
    .await?;
    if newest
        .as_ref()
        .map(|h| h.tree_size <= max_seq)
        .unwrap_or(true)
    {
        store::force_head(pool, tenant).await?;
        newest = sqlx::query_as::<_, HeadRow>(
            "SELECT id, tree_size, statement, transparent_statement FROM evidence_tree_heads \
             WHERE log_id = $1 ORDER BY tree_size DESC LIMIT 1",
        )
        .bind(&log_id)
        .fetch_optional(pool)
        .await?;
    }
    let newest = newest
        .filter(|h| h.tree_size > max_seq)
        .ok_or_else(|| BundleApiError::Internal("no tree head covers the selection".into()))?;
    let size = newest.tree_size as u64;

    // Heads: the first covering each record, and the newest. One index scan
    // for the sizes, one fetch for the chosen statements.
    let min_seq = rows.first().map(|r| r.seq).unwrap_or(0);
    let sizes: Vec<(String, i64)> = sqlx::query_as(
        "SELECT id, tree_size FROM evidence_tree_heads \
         WHERE log_id = $1 AND tree_size > $2 AND tree_size <= $3 ORDER BY tree_size",
    )
    .bind(&log_id)
    .bind(min_seq)
    .bind(newest.tree_size)
    .fetch_all(pool)
    .await?;
    let mut chosen: BTreeSet<String> = BTreeSet::new();
    for r in &rows {
        let i = sizes.partition_point(|(_, ts)| *ts <= r.seq);
        if let Some((id, _)) = sizes.get(i) {
            chosen.insert(id.clone());
        }
    }
    chosen.remove(&newest.id);
    let chosen: Vec<String> = chosen.into_iter().collect();
    let mut heads: Vec<HeadRow> = sqlx::query_as(
        "SELECT id, tree_size, statement, transparent_statement FROM evidence_tree_heads \
         WHERE id = ANY($1)",
    )
    .bind(&chosen)
    .fetch_all(pool)
    .await?;
    heads.push(newest);
    heads.sort_by_key(|h| h.tree_size);
    let head_ids: Vec<String> = heads.iter().map(|h| h.id.clone()).collect();

    // Receipts for the included heads.
    let receipts: Vec<(String, String, Vec<u8>, String)> = sqlx::query_as(
        "SELECT tree_head_id, kind, receipt, verdict FROM evidence_receipts \
         WHERE tree_head_id = ANY($1) AND verdict IN ('witnessed','timestamped') \
         ORDER BY created_at",
    )
    .bind(&head_ids)
    .fetch_all(pool)
    .await?;

    // Load exactly the nodes the proofs read.
    let mut need: Vec<(u32, u64)> = Vec::new();
    for r in &rows {
        need.extend(inclusion_path_nodes(r.seq as u64, size));
    }
    for w in heads.windows(2) {
        need.extend(consistency_proof_nodes(
            w[0].tree_size as u64,
            w[1].tree_size as u64,
        ));
    }
    need.sort();
    need.dedup();
    let mut nodes = MemoryStore::default();
    let mut conn = pool.acquire().await?;
    for chunk in need.chunks(5_000) {
        store::load_nodes(&mut conn, &log_id, chunk, &mut nodes).await?;
    }
    drop(conn);
    let log = Log::open(&mut nodes, size);
    let hexes = |v: Vec<brutor_cose::merkle::Hash>| v.iter().map(hex::encode).collect::<Vec<_>>();
    let mut proofs = Vec::with_capacity(rows.len());
    for r in &rows {
        let path = log
            .inclusion_path(r.seq as u64, size)
            .map_err(|e| BundleApiError::Internal(format!("inclusion path: {e}")))?;
        proofs.push(RecordProof {
            record_id: r.record_id.clone(),
            leaf_index: r.seq as u64,
            tree_size: size,
            path: hexes(path),
        });
    }
    let mut consistency = Vec::new();
    for w in heads.windows(2) {
        let (a, b) = (w[0].tree_size as u64, w[1].tree_size as u64);
        let p = log
            .consistency_proof(a, b)
            .map_err(|e| BundleApiError::Internal(format!("consistency proof: {e}")))?;
        consistency.push(ConsistencyProof {
            from_size: a,
            to_size: b,
            path: hexes(p),
        });
    }

    let bundle_id = format!("bdl-{}", ulid::Ulid::new());
    let input = BundleInput {
        bundle_id: bundle_id.clone(),
        root_record_id,
        selector: req.selector.to_json(),
        produced_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
        issuer: key::issuer(),
        log_id: log_id.clone(),
        framework_id: req.framework_id.clone(),
        catalog_version: req.catalog_version.clone(),
        closure_depth: req.closure_depth,
        selection: req.selector.selection().into(),
        missing: missing.into_iter().collect(),
        records: rows
            .iter()
            .map(|r| BundleRecord {
                record: r.record.clone(),
                statement_b64: enc(&r.statement),
                seq: r.seq as u64,
            })
            .collect(),
        proofs,
        tree_heads: heads
            .iter()
            .map(|h| BundleHead {
                statement_b64: enc(&h.statement),
                transparent_statement_b64: h.transparent_statement.as_deref().map(enc),
                receipts: receipts
                    .iter()
                    .filter(|(hid, _, _, _)| *hid == h.id)
                    .map(|(_, k, r, v)| BundleReceipt {
                        kind: k.clone(),
                        receipt_b64: enc(r),
                        grade: v.clone(),
                    })
                    .collect(),
            })
            .collect(),
        consistency,
        prunes: vec![],
        extensions: req.extensions.clone().unwrap_or_default(),
    };
    let (bundle, digest) = eb::build(input).map_err(|e| BundleApiError::Invalid(e.to_string()))?;
    let bytes = serde_json::to_vec(&bundle).map_err(|e| BundleApiError::Internal(e.to_string()))?;
    if bytes.len() > MAX_BUNDLE_BYTES {
        return Err(BundleApiError::TooLarge(format!(
            "bundle is {} bytes; the cap is {MAX_BUNDLE_BYTES}",
            bytes.len()
        )));
    }
    // Self-check before sealing anything: a bundle this deployment cannot
    // verify is never handed out.
    let check = eb::verify(
        &bundle,
        &VerifyOptions {
            trusted_kids: Some(key::trusted_kids(pool).await.map_err(SealError::from)?),
            expected_digest: Some(digest.clone()),
        },
    );
    if !check.ok {
        return Err(BundleApiError::Internal(format!(
            "assembled bundle failed verification: {:?}",
            check
                .findings
                .iter()
                .filter(|f| f.severity == eb::Severity::Error)
                .map(|f| f.code)
                .collect::<Vec<_>>()
        )));
    }

    // Seal the digest (and the disclosure) in one transaction.
    let completeness = if req.selector.selection() == selection::PRODUCER_SELECTED {
        "producer_selected"
    } else {
        "contiguous"
    };
    let bundle_rec = fyi_record(
        tenant,
        BUNDLE_RECORD_TYPE,
        &bundle_id,
        &req.framework_id,
        &req.catalog_version,
        vec![reference("ai.brutor.bundle", &digest, "bundle_digest")],
    )?;
    let disclosure_id = req
        .audience
        .as_ref()
        .map(|_| format!("dsc-{}", ulid::Ulid::new()));
    let disclosure_rec = match (&req.audience, &disclosure_id) {
        (Some(aud), Some(_)) => {
            let aud_digest = brutor_evidence::json_digest_hex(&json!(aud))
                .map_err(|e| BundleApiError::Internal(e.to_string()))?;
            Some(fyi_record(
                tenant,
                DISCLOSURE_RECORD_TYPE,
                &bundle_id,
                &req.framework_id,
                &req.catalog_version,
                vec![
                    reference("ai.brutor.bundle", &digest, "disclosed_bundle"),
                    reference("ai.brutor.audience", &aud_digest, "audience_label"),
                ],
            )?)
        }
        _ => None,
    };
    let mut items = vec![Item::record(&bundle_rec)];
    if let (Some(m), Some(id)) = (&disclosure_rec, &disclosure_id) {
        items.push(Item {
            minted: m,
            entry_kind: "disclosure",
            ref_id: Some(id.clone()),
        });
    }
    let sealed = store::seal_items(pool, tenant, &items, &signing).await?;
    let bundle_sealed = &sealed[0];

    if let (Some(aud), Some(id), Some(ds)) = (&req.audience, &disclosure_id, sealed.get(1)) {
        let ids: Vec<&str> = rows.iter().map(|r| r.record_id.as_str()).collect();
        sqlx::query(
            "INSERT INTO disclosure_records \
             (id, tenant_id, log_id, seq, audience, selector, completeness, disclosed_record_ids, \
              suppressed_fields, bundle_digest, statement, created_by) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, '[]'::jsonb, $9, $10, $11)",
        )
        .bind(id)
        .bind(tenant)
        .bind(&log_id)
        .bind(ds.seq)
        .bind(aud)
        .bind(req.selector.to_json())
        .bind(completeness)
        .bind(json!(ids))
        .bind(&digest)
        .bind(&ds.statement)
        .bind(&req.requested_by)
        .execute(pool)
        .await?;
    }

    let persisted = match sqlx::query(
        "INSERT INTO evidence_bundles \
         (id, tenant_id, log_id, selector, audience, payloads_mode, selection, framework_id, \
          catalog_version, obligation_key, root_record_id, record_count, bundle_digest, record_id, \
          disclosure_record_id, bundle, size_bytes, requested_by) \
         VALUES ($1, $2, $3, $4, $5, 'none', $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)",
    )
    .bind(&bundle_id)
    .bind(tenant)
    .bind(&log_id)
    .bind(req.selector.to_json())
    .bind(&req.audience)
    .bind(req.selector.selection())
    .bind(&req.framework_id)
    .bind(&req.catalog_version)
    .bind(&req.obligation_key)
    .bind(bundle["manifest"]["root_record_id"].as_str())
    .bind(rows.len() as i32)
    .bind(&digest)
    .bind(&bundle_sealed.record_id)
    .bind(&disclosure_id)
    .bind(&bundle)
    .bind(bytes.len() as i32)
    .bind(&req.requested_by)
    .execute(pool)
    .await
    {
        Ok(_) => true,
        Err(e) => {
            tracing::warn!(
                target = "brutor::evidence",
                bundle_id = %bundle_id,
                error = %e,
                "bundle sealed but not persisted to evidence_bundles",
            );
            false
        }
    };
    Ok(BuiltBundle {
        bundle_id,
        bundle_digest: digest,
        record_id: bundle_sealed.record_id.clone(),
        disclosure_id,
        bundle,
        persisted,
    })
}

fn reference(kind: &str, digest: &str, purpose: &str) -> Reference {
    Reference {
        kind: kind.into(),
        digest_alg: "SHA-256".into(),
        digest: digest.into(),
        purpose: Some(purpose.into()),
    }
}

fn fyi_record(
    tenant: &str,
    record_type: &str,
    bundle_id: &str,
    framework_id: &Option<String>,
    catalog_version: &Option<String>,
    refs: Vec<Reference>,
) -> Result<Minted, BundleApiError> {
    let now = chrono::Utc::now();
    let mut b = RecordBuilder::new(
        format!("{record_type}/{bundle_id}"),
        tenant,
        now.to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
    );
    b.kind = kind::FYI.into();
    b.verdict = verdict::RECORDED.into();
    b.disposition = Some(Disposition {
        decision: decision::ACCEPT.into(),
        approver: approver::POLICY.into(),
        human_disposed: false,
        authority: None,
        reason_digest: None,
    });
    let mut ctx = Map::new();
    ctx.insert("record_type".into(), json!(record_type));
    ctx.insert("bundle_id".into(), json!(bundle_id));
    if let Some(f) = framework_id {
        ctx.insert("framework_id".into(), json!(f));
    }
    if let Some(c) = catalog_version {
        ctx.insert("catalog_version".into(), json!(c));
    }
    b.context = ctx;
    b.references = Some(refs);
    let (record, record_id) = b
        .seal()
        .map_err(|e| BundleApiError::Internal(e.to_string()))?;
    Ok(Minted {
        row_index: 0,
        tenant_id: tenant.to_string(),
        record,
        record_id,
        ai_system_id: None,
        root_task_id: None,
        proxy_log_id: None,
        verdict: verdict::RECORDED.into(),
        kind: kind::FYI.into(),
        surface: "evidence".into(),
        epoch: None,
        timestamp: now,
        tags: None,
        approval_request_id: None,
        consumed_approval_request_id: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn req(v: Value) -> Result<BundleRequest, serde_json::Error> {
        serde_json::from_value(v)
    }

    #[test]
    fn selectors_parse_and_classify() {
        let r = req(json!({"tenant_id":"t","selector":{"record_ids":["a".repeat(64)]}})).unwrap();
        assert_eq!(r.selector.selection(), selection::PRODUCER_SELECTED);
        assert_eq!(r.closure_depth, 1);
        assert_eq!(r.payloads, "none");
        let r = req(json!({"tenant_id":"t","selector":{"root_task_id":"run-1"}})).unwrap();
        assert_eq!(r.selector.selection(), selection::CONTIGUOUS);
        let r = req(json!({"tenant_id":"t","selector":{"ai_system_id":"rg","from":"2026-09-01T00:00:00Z","to":"2026-09-02T00:00:00Z"}})).unwrap();
        assert_eq!(r.selector.selection(), selection::CONTIGUOUS);
        assert!(check(&r).is_ok());
        assert!(req(json!({"tenant_id":"t","selector":{"bogus":1}})).is_err());
    }

    #[test]
    fn refusals() {
        let r =
            req(json!({"tenant_id":"t","selector":{"root_task_id":"r"},"payloads":"all"})).unwrap();
        assert!(matches!(
            check(&r),
            Err(BundleApiError::PayloadsUnavailable)
        ));
        let r = req(json!({"tenant_id":"t","selector":{"record_ids":["nothex"]}})).unwrap();
        assert!(matches!(check(&r), Err(BundleApiError::Invalid(_))));
        let ids: Vec<String> = (0..=MAX_BUNDLE_RECORDS)
            .map(|i| format!("{i:064x}"))
            .collect();
        let r = req(json!({"tenant_id":"t","selector":{"record_ids":ids}})).unwrap();
        assert!(matches!(check(&r), Err(BundleApiError::TooLarge(_))));
        let r = req(json!({"tenant_id":"t","selector":{"root_task_id":"r"},"closure_depth":99}))
            .unwrap();
        assert!(matches!(check(&r), Err(BundleApiError::Invalid(_))));
        let r = req(json!({"tenant_id":"t","selector":{"ai_system_id":"rg","from":"2026-09-02T00:00:00Z","to":"2026-09-01T00:00:00Z"}})).unwrap();
        assert!(matches!(check(&r), Err(BundleApiError::Invalid(_))));
    }
}

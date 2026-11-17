// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Witness registration.
//!
//! A tree head is POSTed to every enabled witness for its tenant (platform
//! witnesses plus the tenant's own). The `scitt` adapter speaks RFC 9943
//! registration as `capsule-anchor` / `brutor-witness` expose it
//! (`POST /transparency/register-statement` with the statement base64 in
//! JSON, receipt base64 back); the receipt is verified offline under the
//! witness's **pinned** key — a key learned from the witness itself is
//! display only, never a trust anchor. Grades never go up: an unpinned
//! witness yields `unverified` (not evidence of forgery), a same-operator
//! witness can only ever make a head `self_witnessed`, and `plural` needs
//! two independently operated witnesses.
//!
//! The `rfc3161` adapter (RFC 3161 Time-Stamp Protocol) POSTs a DER
//! `TimeStampReq` over SHA-256 of the head statement bytes to the TSA's
//! URL and stores the returned `TimeStampToken` as the receipt (kind
//! `rfc3161`). It is verified offline by `brutor-tsp` against the TSA
//! certificate pinned in `witnesses.pinned_key` — either the PEM
//! certificate (the column is TEXT) or the SHA-256 of its
//! DER (64 hex), in which case the certificate is taken from the token and
//! trusted only because its hash matches. Per receipt: `timestamped` /
//! `invalid` / `unverified`.
//!
//! # Head grades
//!
//! `standalone` → `witnessed` (one verified scitt receipt from an
//! independent operator) → `plural` (two or more distinct independent
//! operators); `self_witnessed` when only same-operator witnesses verified.
//! An RFC 3161 token proves *when* a head existed, not that anyone else
//! holds it in an append-only structure, so a `timestamped` receipt never
//! raises the grade: an rfc3161-only head stays `standalone` and the
//! timestamp is surfaced alongside it (its `evidence_receipts` row with
//! verdict `timestamped`, `witness_iat` = genTime, and the bundle's
//! per-head `receipts[]`). The `evidence_unwitnessed` alert therefore still
//! fires for a head that is only timestamped — deliberately.
//!
//! Unwitnessed alerting input (for the control plane, no schema change):
//! `SELECT … FROM evidence_tree_heads WHERE tenant_id = $1 AND issued_at <
//! now() - interval '1 hour' AND grade IN ('standalone','self_witnessed')`
//! — served by `ix_evidence_tree_heads_tenant (tenant_id, issued_at)`.
//!
//! Fail-open for traffic, fail-loud for evidence: nothing here runs on the
//! request path; a witness outage leaves the head `standalone` and the
//! retry loop tries again.

use crate::services::db_errors::DbResultExt;
use crate::services::outbound_policy;
use base64::Engine as _;
use brutor_cose::sign1::CoseSign1;
/// Receipt grading is pure and lives in `brutor-verify`, so the gateway,
/// the offline CLI and the wasm verifier page grade a receipt identically
/// (tests: `crates/brutor-verify/src/receipt.rs`).
pub use brutor_verify::receipt::{Graded, grade_rfc3161, grade_scitt, head_grade};
use sqlx::PgPool;
use std::sync::OnceLock;
use std::time::Duration;

pub const RETRY_INTERVAL_SECS: u64 = 300;

/// Verdict of the receipt row that records a head was NOT submitted to a
/// witness because witness registration is switched off. It is no receipt:
/// it never raises a grade and never goes into a bundle; it says why the
/// head is unwitnessed.
pub const VERDICT_SKIPPED: &str = "skipped";

/// Why: the text of a [`VERDICT_SKIPPED`] row and of its log line.
pub const SKIPPED_REASON: &str = "not submitted: witness registration is switched off \
     (BRUTOR_EVIDENCE_WITNESS_ENABLED=false); the head stays unwitnessed until it is switched \
     on again, when the retry loop submits it";

/// Whether heads are registered with witnesses
/// (`BRUTOR_EVIDENCE_WITNESS_ENABLED`, on unless it says otherwise).
pub fn egress_enabled() -> bool {
    crate::env_vars::process().flag(&crate::env_vars::BRUTOR_EVIDENCE_WITNESS_ENABLED)
}

fn client() -> &'static reqwest::Client {
    static C: OnceLock<reqwest::Client> = OnceLock::new();
    C.get_or_init(|| {
        outbound_policy::client_builder()
            .timeout(Duration::from_secs(20))
            .build()
            .expect("witness http client")
    })
}

#[derive(Debug, sqlx::FromRow)]
struct WitnessRow {
    id: String,
    kind: String,
    url: String,
    pinned_key: Option<String>,
}

#[derive(Debug, Clone)]
pub struct Registered {
    pub receipt: Vec<u8>,
    pub leaf_index: Option<i64>,
    pub tree_size: Option<i64>,
}

/// Register one head with every enabled witness. Idempotent per
/// (head, witness): an existing `witnessed` receipt is left alone.
pub async fn register_head(pool: PgPool, head_id: String) {
    if !egress_enabled() {
        record_skipped(&pool, &head_id).await;
        return;
    }
    let head: Option<(String, String, Vec<u8>)> = sqlx::query_as(
        "SELECT log_id, tenant_id, statement FROM evidence_tree_heads WHERE id = $1",
    )
    .bind(&head_id)
    .fetch_optional(&pool)
    .await
    // Degrade (logged and counted): background; retry_loop re-drives the head.
    .ok_logged()
    .flatten();
    let Some((_log_id, tenant_id, statement)) = head else {
        return;
    };
    // Background work: a failed read is logged and counted and this head is
    // left for `retry_loop`, which re-drives every unwitnessed head.
    let witnesses: Vec<WitnessRow> = sqlx::query_as(
        "SELECT id, kind, url, pinned_key FROM witnesses \
         WHERE enabled AND (scope = 'platform' OR tenant_id = $1)",
    )
    .bind(&tenant_id)
    .fetch_all(&pool)
    .await
    // Degrade (logged and counted): background; retry_loop re-drives the head.
    .or_logged_default();
    if witnesses.is_empty() {
        return;
    }
    for w in witnesses {
        let already: Option<(String,)> = match sqlx::query_as(
            "SELECT verdict FROM evidence_receipts WHERE tree_head_id = $1 AND witness_id = $2 \
             AND verdict IN ('witnessed','timestamped') LIMIT 1",
        )
        .bind(&head_id)
        .bind(&w.id)
        .fetch_optional(&pool)
        .await
        {
            Ok(found) => found,
            Err(e) => {
                // Unknown is not "not yet witnessed": skip, and the
                // retry loop comes back to this head, rather than submitting
                // it to the witness a second time.
                crate::services::db_errors::record(&e);
                continue;
            }
        };
        if already.is_some() {
            continue;
        }
        let outcome = match w.kind.as_str() {
            "scitt" => scitt_register(&w.url, &statement).await,
            "rfc3161" => rfc3161_register(&w.url, &statement).await,
            other => Err(format!("witness kind {other} not supported")),
        };
        match outcome {
            Ok(reg) => {
                let graded = if w.kind == "rfc3161" {
                    grade_rfc3161(&reg.receipt, &statement, w.pinned_key.as_deref())
                } else {
                    grade_scitt(&reg.receipt, &statement, w.pinned_key.as_deref())
                };
                let _ = sqlx::query(
                    "INSERT INTO evidence_receipts \
                     (id, tree_head_id, witness_id, kind, receipt, leaf_index, tree_size, verdict, verified_at, vendor_ext, error, witness_iat) \
                     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, CASE WHEN $8 IN ('witnessed','timestamped') THEN now() END, $9, $10, $11)",
                )
                .bind(format!("rcp-{}", ulid::Ulid::new()))
                .bind(&head_id)
                .bind(&w.id)
                .bind(&w.kind)
                .bind(&reg.receipt)
                .bind(reg.leaf_index.or(graded.leaf_index))
                .bind(reg.tree_size.or(graded.tree_size))
                .bind(graded.verdict)
                .bind(graded.vendor_ext)
                .bind(graded.errors.first().cloned())
                .bind(graded.iat)
                .execute(&pool)
                .await;
            }
            Err(e) => {
                tracing::warn!(
                    target = "brutor::evidence",
                    witness = %w.id,
                    head = %head_id,
                    error = %e,
                    "witness registration failed; head stays unwitnessed until the retry loop succeeds",
                );
                let _ = sqlx::query(
                    "INSERT INTO evidence_receipts (id, tree_head_id, witness_id, kind, receipt, verdict, error) \
                     VALUES ($1, $2, $3, $4, ''::bytea, 'invalid', $5)",
                )
                .bind(format!("rcp-{}", ulid::Ulid::new()))
                .bind(&head_id)
                .bind(&w.id)
                .bind(&w.kind)
                .bind(format!("registration failed: {e}"))
                .execute(&pool)
                .await;
            }
        }
    }
    let _ = regrade(&pool, &head_id, &statement).await;
}

/// With the kill switch off: record, once per (head, enabled witness of its
/// tenant), that the head was not submitted and why, and log it. Background
/// work: a failed write is logged and counted, never raised.
async fn record_skipped(pool: &PgPool, head_id: &str) {
    let recorded = sqlx::query(
        "INSERT INTO evidence_receipts (id, tree_head_id, witness_id, kind, receipt, verdict, error) \
         SELECT 'rcp-' || md5(h.id || ':' || w.id || ':skipped'), h.id, w.id, w.kind, ''::bytea, $2, $3 \
         FROM evidence_tree_heads h \
         JOIN witnesses w ON w.enabled AND (w.scope = 'platform' OR w.tenant_id = h.tenant_id) \
         WHERE h.id = $1 AND NOT EXISTS (SELECT 1 FROM evidence_receipts r \
             WHERE r.tree_head_id = h.id AND r.witness_id = w.id AND r.verdict = $2)",
    )
    .bind(head_id)
    .bind(VERDICT_SKIPPED)
    .bind(SKIPPED_REASON)
    .execute(pool)
    .await;
    match recorded {
        Ok(done) if done.rows_affected() > 0 => tracing::warn!(
            target = "brutor::evidence",
            head = %head_id,
            witnesses = done.rows_affected(),
            "evidence tree head recorded as unwitnessed: {SKIPPED_REASON}",
        ),
        Ok(_) => {}
        // Background work with no caller to answer: counted, and the head
        // is said to be unwitnessed in the log at least.
        Err(e) => {
            crate::services::db_errors::record(&e);
            tracing::warn!(
                target = "brutor::evidence",
                head = %head_id,
                "evidence tree head left unwitnessed and the record of why could not be \
                 written: {SKIPPED_REASON}",
            );
        }
    }
}

async fn scitt_register(url: &str, statement: &[u8]) -> Result<Registered, String> {
    outbound_policy::check_url(url).map_err(|d| format!("outbound policy: {d:?}"))?;
    let endpoint = format!(
        "{}/transparency/register-statement",
        url.trim_end_matches('/')
    );
    let body = serde_json::json!({
        "signed_statement_b64": base64::engine::general_purpose::STANDARD.encode(statement),
    });
    let resp = client()
        .post(&endpoint)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status();
    let text = resp.text().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!(
            "HTTP {status}: {}",
            text.chars().take(200).collect::<String>()
        ));
    }
    let v: serde_json::Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    let receipt_b64 = v
        .get("receipt_b64")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| "response has no receipt_b64".to_string())?;
    let receipt = base64::engine::general_purpose::STANDARD
        .decode(receipt_b64)
        .map_err(|e| format!("receipt_b64: {e}"))?;
    Ok(Registered {
        receipt,
        leaf_index: v.get("leaf_index").and_then(serde_json::Value::as_i64),
        tree_size: v.get("tree_size").and_then(serde_json::Value::as_i64),
    })
}

/// POST a `TimeStampReq` over SHA-256(statement) and return the token.
async fn rfc3161_register(url: &str, statement: &[u8]) -> Result<Registered, String> {
    outbound_policy::check_url(url).map_err(|d| format!("outbound policy: {d:?}"))?;
    let nonce: u64 = rand::random();
    let req = brutor_tsp::build_request(statement, nonce).map_err(|e| e.to_string())?;
    let resp = client()
        .post(url)
        .header(
            reqwest::header::CONTENT_TYPE,
            brutor_tsp::REQUEST_CONTENT_TYPE,
        )
        .header(reqwest::header::ACCEPT, brutor_tsp::RESPONSE_CONTENT_TYPE)
        .body(req)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status();
    let body = resp.bytes().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("HTTP {status}"));
    }
    let token = brutor_tsp::token_from_response(&body).map_err(|e| e.to_string())?;
    // The nonce binds the reply to this request (RFC 3161 §2.4.2); checked
    // here, where the request is known — the stored token is graded later
    // against the pin alone.
    let echoed = brutor_tsp::verify_token(&token, statement, None)
        .info
        .and_then(|i| i.nonce_hex);
    if echoed.as_deref() != Some(hex::encode(brutor_tsp::nonce_bytes(nonce)).as_str()) {
        return Err("TSA reply nonce does not match the request".into());
    }
    Ok(Registered {
        receipt: token,
        leaf_index: None,
        tree_size: None,
    })
}

/// Recompute the head's grade from its receipts and rebuild the
/// Transparent Statement (receipts at label 394).
pub async fn regrade(pool: &PgPool, head_id: &str, statement: &[u8]) -> Result<(), sqlx::Error> {
    let rows: Vec<(String, String, bool, Vec<u8>)> = sqlx::query_as(
        "SELECT COALESCE(r.witness_id, ''), r.verdict, COALESCE(w.same_operator, true), r.receipt \
         FROM evidence_receipts r LEFT JOIN witnesses w ON w.id = r.witness_id \
         WHERE r.tree_head_id = $1",
    )
    .bind(head_id)
    .fetch_all(pool)
    .await?;
    // Only scitt receipts are COSE receipts and go at label 394; an RFC 3161
    // token is carried beside the head, never inside it.
    let receipts: Vec<Vec<u8>> = rows
        .iter()
        .filter(|(_, v, _, _)| v == "witnessed")
        .map(|(_, _, _, r)| r.clone())
        .collect();
    let graded: Vec<(String, String, bool)> = rows
        .iter()
        .map(|(w, v, s, _)| (w.clone(), v.clone(), *s))
        .collect();
    let grade = head_grade(&graded);
    let transparent: Option<Vec<u8>> = if receipts.is_empty() {
        None
    } else {
        CoseSign1::decode(statement).ok().and_then(|mut s| {
            s.attach_receipts(&receipts);
            s.encode().ok()
        })
    };
    sqlx::query(
        "UPDATE evidence_tree_heads SET grade = $2, transparent_statement = $3 WHERE id = $1",
    )
    .bind(head_id)
    .bind(grade)
    .bind(transparent)
    .execute(pool)
    .await?;
    Ok(())
}

/// Periodic retry: every head without a verified receipt from every
/// enabled witness is re-registered. Runs forever; call from a spawned task.
pub async fn retry_loop(pool: PgPool) {
    let mut tick = tokio::time::interval(Duration::from_secs(RETRY_INTERVAL_SECS));
    loop {
        tick.tick().await;
        retry_pending(&pool).await;
    }
}

/// One pass of [`retry_loop`].
pub async fn retry_pending(pool: &PgPool) {
    if !egress_enabled() {
        return;
    }
    let pending: Vec<(String,)> = sqlx::query_as(
        "SELECT h.id FROM evidence_tree_heads h \
         WHERE h.issued_at > now() - interval '30 days' \
           AND EXISTS (SELECT 1 FROM witnesses w WHERE w.enabled \
                       AND (w.scope = 'platform' OR w.tenant_id = h.tenant_id) \
                       AND NOT EXISTS (SELECT 1 FROM evidence_receipts r \
                                       WHERE r.tree_head_id = h.id AND r.witness_id = w.id \
                                         AND r.verdict IN ('witnessed','timestamped'))) \
         ORDER BY h.issued_at ASC LIMIT 200",
    )
    .fetch_all(pool)
    .await
    // Degrade (logged and counted): background; retry_loop re-drives the head.
    .or_logged_default();
    for (id,) in pending {
        register_head(pool.clone(), id).await;
    }
}

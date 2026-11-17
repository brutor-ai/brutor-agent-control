// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Verification of the audit hash chain.
//!
//! [`crate::services::audit_chain`] *produces* tamper-evident records. This
//! module is what makes them tamper-**evident** rather than merely
//! tamper-resistant: without something that checks, the property exists but
//! nobody can demonstrate it, which is the half that closes an audit.
//!
//! Verification lives here, in Rust, and not in the control plane. It has to
//! recompute [`crate::services::audit_chain::canonical_row_bytes`], and a
//! second implementation of that format in Python would eventually drift from
//! this one — at which point honest rows would start failing verification and
//! a real tamper would be indistinguishable from the drift. The control plane
//! *requests* verification the same way it requests a replay run.
//!
//! The control plane now also carries a *reference* implementation
//! (`app/services/audit_chain_canonical.py`) for evidence that has left the
//! database: exported rows, and third parties re-implementing the format
//! from the docs. The drift risk above is closed by construction rather than
//! by having one copy: both repos pin the same fixed rows to the same hashes
//! for every format, and the control plane parses this crate's field and
//! exclusion lists — a divergence fails CI in both, never an honest row.
//!
//! ## What each verdict claims
//!
//! Deliberately five outcomes, not a boolean, because "not verified" covers
//! four materially different situations an operator would act on differently:
//!
//! - [`ChainVerdict::Verified`] — every hash links **and** a valid signed
//!   checkpoint covers the range. The only verdict that asserts integrity.
//! - [`ChainVerdict::Unattested`] — hashes link, but nothing signed them. The
//!   rows are internally consistent and could still have been rewritten
//!   wholesale. This is what a deployment with no signing key gets.
//! - [`ChainVerdict::Gap`] — sequence numbers are missing: rows were deleted.
//! - [`ChainVerdict::Broken`] — a hash does not match. Something changed.
//! - [`ChainVerdict::Unchained`] — no chain columns at all (rows written
//!   before the chain existed). **Not** a failure, and explicitly not a pass.
//!
//! `Unchained` and `Unattested` exist so that absence of evidence is never
//! reported as evidence of integrity — the same discipline the assurance
//! report applies to its `not_covered` section.

use serde::{Deserialize, Serialize};
use sqlx::PgPool;

use crate::services::audit_chain::{
    self, COVERED_SELECT, ChainRange, CoveredFields, SignedCheckpoint,
};

/// Rows examined per call unless the caller says otherwise. Bounded because
/// this walks `proxy_logs`, and an unbounded verify would be a table scan
/// dressed up as an API call.
pub const DEFAULT_SCAN_LIMIT: i64 = 10_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ChainVerdict {
    /// Hashes link and a valid checkpoint covers them.
    Verified,
    /// Hashes link; nothing signed them.
    Unattested,
    /// Sequence numbers are missing, and every missing one was declared in
    /// `audit_chain_prunes` (retention or erasure), with the surviving rows
    /// still linking to the declared tail hashes. Not a statement that the
    /// records are intact — some are gone — but a statement that nothing
    /// went missing without a record of who removed it and why.
    Pruned,
    /// Sequence numbers are missing and at least one was NOT declared —
    /// rows were deleted by something other than a policy.
    Gap,
    /// A hash does not match its contents or its predecessor.
    Broken,
    /// No chain columns. Predates the chain.
    Unchained,
    /// A checkpoint's signature verifies, but against a key this deployment
    /// never trusted. Ranked worst of all: an unsigned chain is a gap in
    /// evidence, whereas this is evidence that someone produced an
    /// attestation we did not authorise.
    Forged,
}

impl ChainVerdict {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Verified => "verified",
            Self::Unattested => "unattested",
            Self::Pruned => "pruned",
            Self::Gap => "gap",
            Self::Broken => "broken",
            Self::Unchained => "unchained",
            Self::Forged => "forged",
        }
    }

    /// Whether this verdict asserts the records are intact. Only one does.
    pub fn is_intact(&self) -> bool {
        matches!(self, Self::Verified)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ChainReport {
    pub chain_id: String,
    pub verdict: ChainVerdict,
    pub rows_checked: i64,
    pub seq_start: Option<i64>,
    pub seq_end: Option<i64>,
    /// First sequence whose hash failed, when the verdict is `Broken`.
    pub first_bad_seq: Option<i64>,
    /// `(after, before)` pairs bracketing each missing run of sequences.
    pub gaps: Vec<(i64, i64)>,
    /// Missing sequences that a prune record accounts for.
    pub pruned_rows: i64,
    /// `(after, before)` pairs for runs containing at least one sequence no
    /// prune record declared. Empty when every gap is a declared prune.
    pub undeclared_gaps: Vec<(i64, i64)>,
    pub checkpoints_checked: i64,
    pub checkpoints_valid: i64,
    /// Checkpoints whose signature verifies against a key this deployment does
    /// not trust. Counted separately because it is not a weaker form of valid
    /// — it is the signature of a forged attestation.
    pub checkpoints_untrusted: i64,
    /// How many of the scanned rows a valid signature
    /// actually covers, counted from `seq_start`. `Verified` requires this to
    /// span the whole scan; anything less reports `Unattested` and this number
    /// says how far the attestation reaches, so partial evidence is described
    /// as partial rather than rounded up.
    pub attested_rows: i64,
    /// True when the scan stopped at the limit rather than at the end of the
    /// chain — so a caller never reads a partial pass as a whole-chain pass.
    pub truncated: bool,
}

/// Verify one chain, starting at `from_seq`.
///
/// Reads at most `limit` rows. The report says whether it was truncated, and
/// callers must not treat a truncated `Verified` as a statement about the
/// whole chain — it is a statement about the range examined.
pub async fn verify_chain(
    pool: &PgPool,
    chain_id: &str,
    from_seq: i64,
    limit: i64,
) -> Result<ChainReport, sqlx::Error> {
    let limit = limit.clamp(1, DEFAULT_SCAN_LIMIT);

    let sql = format!(
        "SELECT {COVERED_SELECT}, chain_seq, prev_hash, row_hash, chain_version \
         FROM proxy_logs \
         WHERE chain_id = $1 AND chain_seq >= $2 \
         ORDER BY chain_seq \
         LIMIT $3"
    );

    let rows: Vec<ScannedRow> = sqlx::query_as::<_, ChainRow>(&sql)
        .bind(chain_id)
        .bind(from_seq)
        .bind(limit + 1) // one extra to detect truncation
        .fetch_all(pool)
        .await?
        .into_iter()
        .map(|r| {
            (
                r.fields,
                r.chain_seq,
                r.prev_hash,
                r.row_hash,
                r.chain_version,
            )
        })
        .collect();

    let truncated = rows.len() as i64 > limit;
    let rows = &rows[..rows.len().min(limit as usize)];

    let mut report = ChainReport {
        chain_id: chain_id.to_string(),
        verdict: ChainVerdict::Unchained,
        rows_checked: rows.len() as i64,
        seq_start: rows.first().map(|r| r.1),
        seq_end: rows.last().map(|r| r.1),
        first_bad_seq: None,
        gaps: Vec::new(),
        pruned_rows: 0,
        undeclared_gaps: Vec::new(),
        checkpoints_checked: 0,
        checkpoints_valid: 0,
        checkpoints_untrusted: 0,
        attested_rows: 0,
        truncated,
    };

    if rows.is_empty() {
        return Ok(report);
    }

    // --- walk the chain ---------------------------------------------------
    let mut prev_row_hash: Option<&str> = None;
    let mut prev_seq: Option<i64> = None;
    // For each gap, the prev_hash the first survivor carries: a declared
    // prune must end in exactly that hash for the survivors to be trusted.
    let mut survivor_prev: Vec<Option<String>> = Vec::new();

    for (fields, seq, prev_hash, row_hash, chain_version) in rows {
        // Gaps first: a missing row is a different fact from a bad hash, and
        // reporting it as a hash failure would send an operator hunting for a
        // tamper that did not happen.
        let mut follows_a_gap = false;
        if let Some(p) = prev_seq {
            if *seq != p + 1 {
                report.gaps.push((p, *seq));
                survivor_prev.push(prev_hash.clone());
                follows_a_gap = true;
            }
        }
        prev_seq = Some(*seq);

        let (Some(stored_prev), Some(stored_hash)) = (prev_hash.as_deref(), row_hash.as_deref())
        else {
            // A row inside a chain with no hash is not "unchained" — the chain
            // claims to cover it. Treat it as broken.
            report.verdict = ChainVerdict::Broken;
            report.first_bad_seq.get_or_insert(*seq);
            continue;
        };

        // The row's own contents must produce its stored hash under the
        // row format, which `chain_version` must name. See
        // `audit_chain::row_matches`.
        let contents_match =
            audit_chain::row_matches(fields, chain_version.as_deref(), stored_prev, stored_hash);
        if !contents_match {
            report.verdict = ChainVerdict::Broken;
            report.first_bad_seq.get_or_insert(*seq);
            continue;
        }
        // ...and its stored predecessor must be the row before it. Checked
        // separately: matching contents with a rewritten prev_hash would mean
        // rows were re-linked rather than edited.
        //
        // Skipped immediately after a gap. This row's predecessor was deleted,
        // so its prev_hash necessarily fails to match the row we *did* read —
        // and that failure is explained by the gap we already recorded, not
        // independent evidence of an edit. Reporting it as `Broken` would bury
        // the fact an operator actually needs ("rows were deleted") under one
        // that is merely its consequence.
        if let Some(expected) = prev_row_hash {
            if !follows_a_gap && stored_prev != expected {
                report.verdict = ChainVerdict::Broken;
                report.first_bad_seq.get_or_insert(*seq);
            }
        }
        prev_row_hash = Some(stored_hash);
    }

    // --- checkpoints ------------------------------------------------------
    let checkpoints = load_checkpoints(
        pool,
        chain_id,
        report.seq_start.unwrap_or(0),
        report.seq_end.unwrap_or(0),
    )
    .await?;
    report.checkpoints_checked = checkpoints.len() as i64;
    // Ranges of the checkpoints that verified, for the coverage test below.
    let mut attested: Vec<(i64, i64)> = Vec::new();
    for cp in &checkpoints {
        match audit_chain::verify_checkpoint_trust(cp) {
            audit_chain::CheckpointTrust::Trusted => {}
            audit_chain::CheckpointTrust::UntrustedKey => {
                report.checkpoints_untrusted += 1;
                continue;
            }
            audit_chain::CheckpointTrust::BadSignature => continue,
        }
        // A valid signature over a head hash we did not observe attests to
        // some other set of rows, so it says nothing about these.
        let observed = rows
            .iter()
            .find(|(_, seq, _, _, _)| *seq == cp.range.seq_end)
            .and_then(|(_, _, _, h, _)| h.as_deref());
        if observed == Some(cp.range.head_hash.as_str()) {
            report.checkpoints_valid += 1;
            // Remember WHICH rows this checkpoint attests
            // to, not merely that one verified.
            attested.push((cp.range.seq_start, cp.range.seq_end));
        }
    }

    // `Verified` requires the signatures to cover the rows
    // we actually read.
    //
    // The ladder below used to promote on `checkpoints_valid > 0`: a single
    // valid checkpoint anywhere in the range marked the whole scan verified.
    // A ten-thousand-row scan whose only signed checkpoint attests to the
    // first five hundred reported `Verified` for all of them — which is the
    // one thing this module says it never does, because the verdict is what
    // an auditor reads as "these rows are attested". Unattested rows are now
    // counted, and a partially covered scan reports `Unattested` rather than
    // borrowing another range's signature.
    attested.sort_unstable();
    let mut covered_to: Option<i64> = None;
    let mut fully_covered = true;
    if let (Some(start), Some(end)) = (report.seq_start, report.seq_end) {
        let mut cursor = start;
        for (a, b) in &attested {
            if *a > cursor {
                fully_covered = false;
                break;
            }
            if *b >= cursor {
                cursor = *b + 1;
            }
        }
        if cursor <= end {
            fully_covered = false;
        }
        covered_to = Some(cursor.min(end + 1));
    }
    report.attested_rows = match (report.seq_start, covered_to) {
        (Some(start), Some(c)) => (c - start).max(0),
        _ => 0,
    };

    // --- verdict (worst-of) -----------------------------------------------
    // Broken already latched above and outranks everything: a chain with both
    // a bad hash and a gap is broken, not merely gappy.
    // A checkpoint signed by a key we never trusted outranks everything,
    // including a hash break: broken hashes can be corruption, but an
    // unauthorised signature is deliberate.
    // Reconcile gaps against declared prunes.
    if !report.gaps.is_empty() {
        let prunes = load_prunes(
            pool,
            chain_id,
            report.seq_start.unwrap_or(0),
            report.seq_end.unwrap_or(0),
        )
        .await?;
        for (i, (after, before)) in report.gaps.iter().enumerate() {
            let declared = declared_span(&prunes, *after, *before);
            let survivor = survivor_prev.get(i).cloned().flatten();
            match declared {
                // The survivor must link to the declared tail. A survivor with
                // no prev_hash at all is already reported as Broken above.
                Some(tail) if survivor.as_deref() == Some(tail.as_str()) => {
                    report.pruned_rows += before - after - 1;
                }
                _ => report.undeclared_gaps.push((*after, *before)),
            }
        }
    }

    if report.checkpoints_untrusted > 0 {
        report.verdict = ChainVerdict::Forged;
    } else if report.verdict != ChainVerdict::Broken {
        report.verdict = if !report.undeclared_gaps.is_empty() {
            ChainVerdict::Gap
        } else if !report.gaps.is_empty() {
            ChainVerdict::Pruned
        } else if report.checkpoints_valid > 0 && fully_covered {
            ChainVerdict::Verified
        } else {
            // Valid signatures that do not span the scan are reported as
            // `Unattested` with `attested_rows` saying how far they reach:
            // partial evidence, described as partial.
            ChainVerdict::Unattested
        };
    }

    Ok(report)
}

/// Chains present in `proxy_logs`, most recent first.
pub async fn list_chains(pool: &PgPool, limit: i64) -> Result<Vec<String>, sqlx::Error> {
    let rows: Vec<(String,)> = sqlx::query_as(
        "SELECT DISTINCT chain_id FROM proxy_logs \
         WHERE chain_id IS NOT NULL ORDER BY chain_id DESC LIMIT $1",
    )
    .bind(limit.clamp(1, 1000))
    .fetch_all(pool)
    .await?;
    Ok(rows.into_iter().map(|(c,)| c).collect())
}

/// One contiguous removed run from `audit_chain_prunes.runs`.
#[derive(Debug, Clone, Deserialize)]
struct PruneRun {
    from: i64,
    to: i64,
    #[serde(default)]
    tail_hash: String,
}

/// Prune records overlapping `[seq_start, seq_end]`, flattened to runs.
async fn load_prunes(
    pool: &PgPool,
    chain_id: &str,
    seq_start: i64,
    seq_end: i64,
) -> Result<Vec<PruneRun>, sqlx::Error> {
    let rows: Vec<(serde_json::Value,)> = sqlx::query_as(
        "SELECT runs FROM audit_chain_prunes \
         WHERE chain_id = $1 AND seq_max >= $2 AND seq_min <= $3",
    )
    .bind(chain_id)
    .bind(seq_start)
    .bind(seq_end)
    .fetch_all(pool)
    .await?;
    let mut out = Vec::new();
    for (raw,) in rows {
        if let Ok(runs) = serde_json::from_value::<Vec<PruneRun>>(raw) {
            out.extend(runs);
        }
    }
    Ok(out)
}

/// Is every sequence strictly between `after` and `before` covered by some
/// declared run? Returns the tail hash of the run that ends at `before - 1`
/// (what the first survivor must link to), or `None` if any sequence is
/// undeclared. Runs from several prune records may together cover one gap —
/// a tenant-scoped prune and a later erasure can both bite the same chain.
fn declared_span(runs: &[PruneRun], after: i64, before: i64) -> Option<String> {
    // Interval coverage, not a walk over every number.
    //
    // This used to iterate every missing sequence in the gap and do a linear
    // scan of the declared runs for each one. A retention pass that removes a
    // month of a busy tenant's rows leaves a gap of millions, so verifying one
    // such chain turned into millions of scans — on the path an operator hits
    // when they are already asking whether their audit trail is intact.
    //
    // Same answer, computed as coverage: sort the runs, merge the ones that
    // touch, then check the gap is inside one merged span. The tail hash still
    // comes from the run covering `before - 1`, because that is what the
    // survivor must link to.
    if before <= after + 1 {
        return None; // no missing sequences — nothing to declare
    }
    let (first, last) = (after + 1, before - 1);

    let mut spans: Vec<(i64, i64, &str)> = runs
        .iter()
        .map(|r| (r.from, r.to, r.tail_hash.as_str()))
        .collect();
    spans.sort_unstable_by_key(|(from, _, _)| *from);

    let mut cursor = first;
    let mut tail: Option<String> = None;
    for (from, to, hash) in spans {
        if from > cursor {
            break; // a hole the declarations do not cover
        }
        if to >= cursor {
            if (from..=to).contains(&last) {
                tail = Some(hash.to_string());
            }
            cursor = to + 1;
        }
        if cursor > last {
            break;
        }
    }
    if cursor > last { tail } else { None }
}

async fn load_checkpoints(
    pool: &PgPool,
    chain_id: &str,
    seq_start: i64,
    seq_end: i64,
) -> Result<Vec<SignedCheckpoint>, sqlx::Error> {
    let rows: Vec<(String, i64, i64, String, String, String)> = sqlx::query_as(
        "SELECT chain_id, seq_start, seq_end, head_hash, signature, public_key \
         FROM audit_chain_checkpoints \
         WHERE chain_id = $1 AND seq_end >= $2 AND seq_end <= $3 \
         ORDER BY seq_end",
    )
    .bind(chain_id)
    .bind(seq_start)
    .bind(seq_end)
    .fetch_all(pool)
    .await?;

    Ok(rows
        .into_iter()
        .map(
            |(chain_id, seq_start, seq_end, head_hash, signature, public_key)| SignedCheckpoint {
                range: ChainRange {
                    chain_id,
                    seq_start,
                    seq_end,
                    head_hash,
                },
                signature,
                public_key,
            },
        )
        .collect())
}

/// `(fields, chain_seq, prev_hash, row_hash, chain_version)`.
type ScannedRow = (
    CoveredFields,
    i64,
    Option<String>,
    Option<String>,
    Option<String>,
);

/// Row shape for the verification scan.
#[derive(sqlx::FromRow)]
struct ChainRow {
    #[sqlx(flatten)]
    fields: CoveredFields,
    chain_seq: i64,
    prev_hash: Option<String>,
    row_hash: Option<String>,
    /// The format the row says it was sealed under.
    chain_version: Option<String>,
}

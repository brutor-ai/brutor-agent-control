// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Chain verification against a real Postgres.
//!
//! The unit tests prove the hashing rules over fixtures. What only a database
//! can prove is that the verifier detects real tampering *as an auditor would
//! encounter it*: rows already written, then altered by someone with SQL
//! access and no knowledge of the chain.
//!
//! Each case therefore writes a genuine chain through the writer, then mutates
//! Postgres directly — not the in-memory structs.

use std::time::Duration;

use sqlx::PgPool;
use ulid::Ulid;

use brutor_gateway_core::services::audit_verify::{ChainVerdict, verify_chain};
use brutor_gateway_core::services::background_writer::BackgroundWriter;
use brutor_gateway_core::services::proxy_logger;

use super::background_writer_chain::test_auth;
use super::common;

/// Write `n` rows through the writer and return the chain id they landed on.
async fn seed_chain(pool: &PgPool, tenant: &str, n: usize) -> String {
    let writer = BackgroundWriter::new();
    for _ in 0..n {
        let auth = test_auth(tenant);
        let log = proxy_logger::build_proxy_log(
            "llm",
            &auth,
            "POST",
            "/v1/chat/completions",
            None,
            None,
            200,
            3.0,
            true,
            None,
            &format!("corr-{}", Ulid::new()),
        );
        writer.write_proxy_log(pool.clone(), log);
    }

    for _ in 0..40 {
        let row: Option<(String, i64)> = sqlx::query_as(
            "SELECT chain_id, COUNT(*) FROM proxy_logs \
             WHERE tenant_id = $1 AND chain_id IS NOT NULL \
             GROUP BY chain_id LIMIT 1",
        )
        .bind(tenant)
        .fetch_optional(pool)
        .await
        .expect("query");
        if let Some((chain, count)) = row
            && count as usize >= n
        {
            return chain;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("chain never reached {n} rows");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_untouched_chain_verifies(pool: PgPool) {
    let t = "t-av-clean";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 4).await;

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");

    assert_eq!(r.rows_checked, 4);
    assert!(r.gaps.is_empty());
    assert_eq!(r.first_bad_seq, None);
    // No signing key is configured in tests, so the honest verdict is
    // "hashes link, nothing signed them" — NOT "verified".
    assert_eq!(
        r.verdict,
        ChainVerdict::Unattested,
        "an unsigned chain must not claim to be verified",
    );
    assert!(!r.verdict.is_intact());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn editing_a_row_is_detected(pool: PgPool) {
    // The whole point. Someone with SQL access flips a denied call to
    // successful, which is exactly the edit an audit would care about.
    let t = "t-av-tamper";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 4).await;

    let victim: (i64,) = sqlx::query_as(
        "SELECT chain_seq FROM proxy_logs WHERE chain_id = $1 ORDER BY chain_seq OFFSET 1 LIMIT 1",
    )
    .bind(&chain)
    .fetch_one(&pool)
    .await
    .expect("pick a victim row");

    sqlx::query("UPDATE proxy_logs SET success = false WHERE chain_id = $1 AND chain_seq = $2")
        .bind(&chain)
        .bind(victim.0)
        .execute(&pool)
        .await
        .expect("tamper");

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");

    assert_eq!(r.verdict, ChainVerdict::Broken);
    assert_eq!(
        r.first_bad_seq,
        Some(victim.0),
        "the report must name the row that changed, not just that something did",
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_governance_decision_cannot_be_quietly_rewritten(pool: PgPool) {
    // The edit with the strongest motive: turning a deny into an allow.
    let t = "t-av-decision";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 3).await;

    sqlx::query(
        "UPDATE proxy_logs SET agent_authz_decision = 'allow', autonomy_level = 'autonomous' \
         WHERE chain_id = $1 AND chain_seq = (SELECT MIN(chain_seq) FROM proxy_logs WHERE chain_id = $1)",
    )
    .bind(&chain)
    .execute(&pool)
    .await
    .expect("tamper");

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(r.verdict, ChainVerdict::Broken);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn deleting_a_row_is_reported_as_a_gap_not_a_tamper(pool: PgPool) {
    // A deletion and an edit are different facts and lead an operator to
    // different places, so they must not collapse into one verdict.
    let t = "t-av-gap";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 4).await;

    sqlx::query(
        "DELETE FROM proxy_logs WHERE chain_id = $1 AND chain_seq = \
         (SELECT MIN(chain_seq) + 1 FROM proxy_logs WHERE chain_id = $1)",
    )
    .bind(&chain)
    .execute(&pool)
    .await
    .expect("delete");

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");

    assert_eq!(r.verdict, ChainVerdict::Gap);
    assert_eq!(r.gaps.len(), 1, "one missing row is one gap: {:?}", r.gaps);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tamper_outranks_a_gap(pool: PgPool) {
    // Worst-of, like every other aggregate in the platform. A chain that is
    // both edited and missing rows must not report as merely gappy.
    let t = "t-av-both";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 5).await;

    sqlx::query(
        "DELETE FROM proxy_logs WHERE chain_id = $1 AND chain_seq = \
         (SELECT MIN(chain_seq) + 1 FROM proxy_logs WHERE chain_id = $1)",
    )
    .bind(&chain)
    .execute(&pool)
    .await
    .expect("delete");
    sqlx::query(
        "UPDATE proxy_logs SET success = false WHERE chain_id = $1 AND chain_seq = \
         (SELECT MAX(chain_seq) FROM proxy_logs WHERE chain_id = $1)",
    )
    .bind(&chain)
    .execute(&pool)
    .await
    .expect("tamper");

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(r.verdict, ChainVerdict::Broken);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn relinking_a_row_is_detected_even_with_untouched_contents(pool: PgPool) {
    // Contents unchanged, prev_hash rewritten — an attempt to splice rows into
    // a different order. Checking contents alone would miss it.
    let t = "t-av-relink";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 3).await;

    sqlx::query(
        "UPDATE proxy_logs SET prev_hash = $2 WHERE chain_id = $1 AND chain_seq = \
         (SELECT MAX(chain_seq) FROM proxy_logs WHERE chain_id = $1)",
    )
    .bind(&chain)
    .bind("f".repeat(64))
    .execute(&pool)
    .await
    .expect("relink");

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(r.verdict, ChainVerdict::Broken);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_chain_with_no_rows_is_unchained_not_verified(pool: PgPool) {
    // Absence of evidence must never read as evidence of integrity.
    common::seed_tenant(&pool, "t-av-empty").await;

    let r = verify_chain(&pool, "no-such-chain", 0, 100)
        .await
        .expect("verify");

    assert_eq!(r.verdict, ChainVerdict::Unchained);
    assert_eq!(r.rows_checked, 0);
    assert!(!r.verdict.is_intact());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_truncated_scan_says_so(pool: PgPool) {
    // A caller must not read a partial pass as a whole-chain pass.
    let t = "t-av-trunc";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 5).await;

    let r = verify_chain(&pool, &chain, 0, 2).await.expect("verify");
    assert_eq!(r.rows_checked, 2);
    assert!(r.truncated, "a limited scan must report truncation");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_checkpoint_signed_by_an_untrusted_key_is_reported_as_forged(pool: PgPool) {
    // The full attack, end to end: someone with SQL access rewrites a row,
    // recomputes the chain so every hash links, generates their own keypair,
    // and writes a checkpoint attesting to the result. Without a trust anchor
    // this verifies perfectly. With one, the key gives it away.
    //
    // Ranked above `broken` on purpose: a hash mismatch can be corruption,
    // but an attestation signed by a key we never issued is deliberate.
    let t = "t-av-forged";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 3).await;

    let head: (i64, String) = sqlx::query_as(
        "SELECT chain_seq, row_hash FROM proxy_logs \
         WHERE chain_id = $1 ORDER BY chain_seq DESC LIMIT 1",
    )
    .bind(&chain)
    .fetch_one(&pool)
    .await
    .expect("head row");

    // A key this deployment has never heard of, with a genuine signature over
    // the real head hash — so nothing about the maths is wrong.
    let attacker = ed25519_dalek::SigningKey::from_bytes(&[42u8; 32]);
    let range = brutor_gateway_core::services::audit_chain::ChainRange {
        chain_id: chain.clone(),
        seq_start: 0,
        seq_end: head.0,
        head_hash: head.1.clone(),
    };
    let bytes = brutor_gateway_core::services::audit_chain::canonical_checkpoint_bytes(&range);
    let sig = ed25519_dalek::Signer::sign(&attacker, &bytes);

    sqlx::query(
        "INSERT INTO audit_chain_checkpoints \
         (id, chain_id, seq_start, seq_end, head_hash, signature, public_key) \
         VALUES ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(format!("ckpt-forged-{}", Ulid::new()))
    .bind(&chain)
    .bind(0i64)
    .bind(head.0)
    .bind(&head.1)
    .bind(hex::encode(sig.to_bytes()))
    .bind(hex::encode(attacker.verifying_key().to_bytes()))
    .execute(&pool)
    .await
    .expect("insert forged checkpoint");

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");

    assert_eq!(
        r.verdict,
        ChainVerdict::Forged,
        "a valid signature from an untrusted key must outrank every other verdict",
    );
    assert!(
        r.checkpoints_untrusted >= 1,
        "the forged checkpoint must be counted, not silently ignored",
    );
    assert_eq!(
        r.checkpoints_valid, 0,
        "an untrusted checkpoint is not a weaker form of valid",
    );
}

// ---------------------------------------------------------------------------
// Declared prunes
// ---------------------------------------------------------------------------

/// Delete `seq` and, when `declare` is set, record it the way the control
/// plane's retention sweeper does.
async fn prune_row(pool: &PgPool, chain: &str, seq: i64, declare: bool, tail_hash: Option<&str>) {
    let hash: (Option<String>,) =
        sqlx::query_as("SELECT row_hash FROM proxy_logs WHERE chain_id = $1 AND chain_seq = $2")
            .bind(chain)
            .bind(seq)
            .fetch_one(pool)
            .await
            .expect("row hash");
    sqlx::query("DELETE FROM proxy_logs WHERE chain_id = $1 AND chain_seq = $2")
        .bind(chain)
        .bind(seq)
        .execute(pool)
        .await
        .expect("delete");
    if declare {
        let tail = tail_hash.map(String::from).or(hash.0).unwrap_or_default();
        let runs = serde_json::json!([{"from": seq, "to": seq, "tail_hash": tail}]);
        sqlx::query(
            "INSERT INTO audit_chain_prunes \
             (id, chain_id, tenant_id, seq_min, seq_max, row_count, runs, reason, actor) \
             VALUES ($1, $2, NULL, $3, $3, 1, $4, 'retention', 'test')",
        )
        .bind(format!("prune-{}", Ulid::new()))
        .bind(chain)
        .bind(seq)
        .bind(runs)
        .execute(pool)
        .await
        .expect("declare");
    }
}

async fn second_seq(pool: &PgPool, chain: &str) -> i64 {
    let r: (i64,) = sqlx::query_as("SELECT MIN(chain_seq) + 1 FROM proxy_logs WHERE chain_id = $1")
        .bind(chain)
        .fetch_one(pool)
        .await
        .expect("seq");
    r.0
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_declared_deletion_is_pruned_not_a_gap(pool: PgPool) {
    // The retention sweeper removes a row and says so. The verifier must
    // distinguish that from a deletion nobody owned up to, or retention
    // policies would degrade the evidence grade of everything that remains.
    let t = "t-av-pruned";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 4).await;
    let seq = second_seq(&pool, &chain).await;

    prune_row(&pool, &chain, seq, true, None).await;

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(r.verdict, ChainVerdict::Pruned, "{r:?}");
    assert_eq!(r.gaps.len(), 1);
    assert!(r.undeclared_gaps.is_empty());
    assert_eq!(r.pruned_rows, 1);
    assert!(
        !r.verdict.is_intact(),
        "pruned is not a claim that the records are intact"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_undeclared_deletion_is_still_a_gap(pool: PgPool) {
    let t = "t-av-undeclared";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 4).await;
    let seq = second_seq(&pool, &chain).await;

    prune_row(&pool, &chain, seq, false, None).await;

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(r.verdict, ChainVerdict::Gap);
    assert_eq!(r.undeclared_gaps, r.gaps);
    assert_eq!(r.pruned_rows, 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_prune_whose_tail_the_survivor_does_not_link_to_is_a_gap(pool: PgPool) {
    // A prune record is only trusted if the row after the hole still carries
    // the declared tail hash as its prev_hash. Otherwise a deleter could
    // declare a prune for a run it also tampered with.
    let t = "t-av-badtail";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 4).await;
    let seq = second_seq(&pool, &chain).await;

    prune_row(&pool, &chain, seq, true, Some("0000not-the-hash")).await;

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(r.verdict, ChainVerdict::Gap, "{r:?}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tamper_still_outranks_a_declared_prune(pool: PgPool) {
    let t = "t-av-prune-tamper";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 5).await;
    let seq = second_seq(&pool, &chain).await;
    prune_row(&pool, &chain, seq, true, None).await;
    sqlx::query(
        "UPDATE proxy_logs SET success = false WHERE chain_id = $1 AND chain_seq = \
         (SELECT MAX(chain_seq) FROM proxy_logs WHERE chain_id = $1)",
    )
    .bind(&chain)
    .execute(&pool)
    .await
    .expect("tamper");

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(r.verdict, ChainVerdict::Broken);
}

// ---------------------------------------------------------------------------
// The row format: one, named by every row.
// ---------------------------------------------------------------------------

use brutor_gateway_core::services::audit_chain::{
    self, COVERED_SELECT, CoveredFields, GENESIS_HASH,
};

#[derive(sqlx::FromRow)]
struct ScanRow {
    #[sqlx(flatten)]
    fields: CoveredFields,
    chain_seq: i64,
}

/// The verifier computes one row format. A chain whose rows were hashed any
/// other way (here: a hash of other bytes, under no label), re-linked so
/// that every link holds, is broken at its first row: a row is never
/// verified under a format the verifier has to guess. An older label cannot
/// even be stored: the database admits only the format the verifier
/// computes (`ck_proxy_logs_chain_version`).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_chain_sealed_under_another_format_does_not_verify(pool: PgPool) {
    let t = "t-av-other-format";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 4).await;
    let sql = format!(
        "SELECT {COVERED_SELECT}, chain_seq FROM proxy_logs WHERE chain_id = $1 ORDER BY chain_seq"
    );
    let rows: Vec<ScanRow> = sqlx::query_as(&sql)
        .bind(&chain)
        .fetch_all(&pool)
        .await
        .expect("rows");
    for label in ["v1", "v2", "v3", "v4"] {
        let refused = sqlx::query(
            "UPDATE proxy_logs SET chain_version = $2 WHERE chain_id = $1 AND chain_seq = $3",
        )
        .bind(&chain)
        .bind(label)
        .bind(rows[0].chain_seq)
        .execute(&pool)
        .await
        .expect_err("an older format label is refused by the database");
        assert_eq!(
            refused
                .as_database_error()
                .and_then(|e| e.code())
                .as_deref(),
            Some("23514"),
            "label {label}: {refused}"
        );
    }
    {
        let label: Option<&str> = None;
        let mut prev = GENESIS_HASH.to_string();
        for r in &rows {
            // The row format's own hash, under a label that is not its
            // own: the strongest case for a verifier that would guess.
            let h = audit_chain::hash_fields(&r.fields, &prev);
            sqlx::query(
                "UPDATE proxy_logs SET prev_hash = $3, row_hash = $4, chain_version = $5 \
                 WHERE chain_id = $1 AND chain_seq = $2",
            )
            .bind(&chain)
            .bind(r.chain_seq)
            .bind(&prev)
            .bind(&h)
            .bind(label)
            .execute(&pool)
            .await
            .expect("relabel");
            prev = h;
        }
        let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
        assert_eq!(r.verdict, ChainVerdict::Broken, "label {label:?}: {r:?}");
        assert_eq!(r.first_bad_seq, Some(rows[0].chain_seq), "label {label:?}");
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_content_digest_outlives_its_body_and_cannot_be_rewritten(pool: PgPool) {
    let t = "t-av-digest";
    common::seed_tenant(&pool, t).await;

    let writer = BackgroundWriter::new();
    for i in 0..3 {
        let auth = test_auth(t);
        let mut log = proxy_logger::build_proxy_log(
            "mcp",
            &auth,
            "POST",
            "mcp-proxy",
            None,
            None,
            200,
            2.0,
            true,
            None,
            &format!("corr-{}", Ulid::new()),
        );
        log.request_body_preview = Some(format!(
            "{{\"method\":\"tools/call\",\"params\":{{\"name\":\"refund\",\"arguments\":{{\"n\":{i}}}}}}}"
        ));
        log.response_body_preview = Some("{\"result\":{\"content\":[]}}".to_string());
        writer.write_proxy_log(pool.clone(), log);
    }
    let mut chain = None;
    for _ in 0..40 {
        let row: Option<(String, i64)> = sqlx::query_as(
            "SELECT chain_id, COUNT(*) FROM proxy_logs WHERE tenant_id = $1 AND chain_id IS NOT NULL GROUP BY chain_id LIMIT 1",
        )
        .bind(t)
        .fetch_optional(&pool)
        .await
        .expect("query");
        if let Some((c, n)) = row.filter(|(_, n)| *n >= 3) {
            let _ = n;
            chain = Some(c);
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let chain = chain.expect("rows never landed");

    let (seq, digest): (i64, Option<String>) = sqlx::query_as(
        "SELECT chain_seq, request_content_hash FROM proxy_logs WHERE chain_id = $1 ORDER BY chain_seq OFFSET 1 LIMIT 1",
    )
    .bind(&chain)
    .fetch_one(&pool)
    .await
    .expect("row");
    assert_eq!(
        digest.as_deref().map(str::len),
        Some(64),
        "digest sealed at write"
    );

    // Retention removes the bodies, as the sweeper does. The digest — and
    // the chain — must not notice.
    sqlx::query(
        "UPDATE proxy_logs SET request_body_preview = NULL, response_body_preview = NULL, bodies_pruned_at = now() WHERE chain_id = $1",
    )
    .bind(&chain)
    .execute(&pool)
    .await
    .expect("prune");
    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_ne!(
        r.verdict,
        ChainVerdict::Broken,
        "pruning a body is not a tamper"
    );

    // Rewriting what the content *was*, after the content is gone, is.
    sqlx::query("UPDATE proxy_logs SET request_content_hash = repeat('0', 64) WHERE chain_id = $1 AND chain_seq = $2")
        .bind(&chain)
        .bind(seq)
        .execute(&pool)
        .await
        .expect("tamper");
    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(r.verdict, ChainVerdict::Broken);
    assert_eq!(r.first_bad_seq, Some(seq));
}

// ---------------------------------------------------------------------------
// A row that never landed must not read as a deletion
// ---------------------------------------------------------------------------
//
// This interaction is pinned here. Sequence numbers
// are reserved and the chain head advanced BEFORE the insert — that ordering
// is what makes the chain tamper-evident, because a shed row cannot renumber
// the ones after it. The cost is that a failed insert leaves a hole, and a
// hole is indistinguishable from a deletion unless somebody declares it.
//
// The stakes are why this deserves a test of its own: `Gap` means "rows were
// removed by something other than a policy", which is the one verdict an
// auditor is meant to escalate on. A transient database blip must not be able
// to manufacture that accusation.

/// Force one row of a batch to be rejected by Postgres.
///
/// `proxy_subtype` is `varchar(50)`, so an oversized value fails the insert
/// on that row and only that row. Chosen deliberately over an unreachable
/// database: this reproduces the *partial* failure, where the batch insert
/// fails, the per-row fallback lands the good rows, and exactly one sequence
/// is burned without a row — which is the case the declaration exists for.
fn oversized_subtype() -> String {
    "x".repeat(200)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_row_the_database_rejected_is_declared_not_reported_as_a_gap(pool: PgPool) {
    let t = "t-av-writefail";
    common::seed_tenant(&pool, t).await;

    let writer = BackgroundWriter::new();
    // Four rows in one batch, the third of which cannot be inserted.
    for i in 0..4 {
        let auth = test_auth(t);
        let mut log = proxy_logger::build_proxy_log(
            "llm",
            &auth,
            "POST",
            "/v1/chat/completions",
            None,
            None,
            200,
            3.0,
            true,
            None,
            &format!("corr-{}", Ulid::new()),
        );
        if i == 2 {
            log.proxy_subtype = Some(oversized_subtype());
        }
        writer.write_proxy_log(pool.clone(), log);
    }

    // Wait for the three writable rows to land AND for the declaration of the
    // fourth. Polling both, because the declaration is written after the
    // per-row fallback finishes.
    let mut chain = None;
    for _ in 0..60 {
        let row: Option<(String, i64)> = sqlx::query_as(
            "SELECT chain_id, COUNT(*) FROM proxy_logs \
             WHERE tenant_id = $1 AND chain_id IS NOT NULL \
             GROUP BY chain_id LIMIT 1",
        )
        .bind(t)
        .fetch_optional(&pool)
        .await
        .expect("query");
        let declared: (i64,) = sqlx::query_as(
            "SELECT COUNT(*) FROM audit_chain_prunes \
             WHERE tenant_id = $1 AND reason = 'write_failed'",
        )
        .bind(t)
        .fetch_one(&pool)
        .await
        .expect("query");
        if let Some((c, count)) = row
            && count >= 3
            && declared.0 >= 1
        {
            chain = Some(c);
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let chain = chain.expect("three rows landed and the failed one was declared");

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");

    assert!(
        r.undeclared_gaps.is_empty(),
        "an infrastructure failure must never read as an undeclared deletion; got {:?}",
        r.undeclared_gaps
    );
    assert_ne!(
        r.verdict,
        ChainVerdict::Gap,
        "a write that never happened is declared, so the chain is `pruned`, not `gap`"
    );
    assert_ne!(
        r.verdict,
        ChainVerdict::Broken,
        "the surviving rows must still link through the declared tail hash"
    );
    assert!(
        r.pruned_rows >= 1,
        "the burned sequence should be counted as accounted-for, not merely absent"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_undeclared_deletion_still_reads_as_a_gap(pool: PgPool) {
    // The control for the test above: the declaration must be doing the work,
    // not the verifier having gone soft on holes generally.
    let t = "t-av-realgap";
    common::seed_tenant(&pool, t).await;
    let chain = seed_chain(&pool, t, 4).await;

    sqlx::query(
        "DELETE FROM proxy_logs WHERE chain_id = $1 AND chain_seq = \
         (SELECT MIN(chain_seq) + 1 FROM proxy_logs WHERE chain_id = $1)",
    )
    .bind(&chain)
    .execute(&pool)
    .await
    .expect("delete");

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(
        r.verdict,
        ChainVerdict::Gap,
        "a row removed with no prune record is exactly what `gap` is for"
    );
    assert!(!r.undeclared_gaps.is_empty());
}

// ---------------------------------------------------------------------------
// The release fields, the deciding model and the fixed-width timestamp.
// ---------------------------------------------------------------------------

/// Write `n` rows carrying every release field and a deciding model, and
/// return the chain they landed on.
async fn seed_release_chain(pool: &PgPool, tenant: &str, n: usize) -> String {
    let writer = BackgroundWriter::new();
    for i in 0..n {
        let auth = test_auth(tenant);
        let mut log = proxy_logger::build_proxy_log(
            "mcp",
            &auth,
            "POST",
            "mcp-proxy",
            None,
            None,
            200,
            2.0,
            true,
            None,
            &format!("corr-{}", Ulid::new()),
        );
        log.agent_impl_name = Some("support-copilot".into());
        log.agent_impl_version = Some(format!("1.4.{i}"));
        log.agent_impl_build = Some("sha256-abc".into());
        log.agent_impl_source = Some("header".into());
        log.client_label = Some("python-httpx".into());
        log.deciding_model = Some("gpt-5.2".into());
        set_json_records(&mut log, i);
        writer.write_proxy_log(pool.clone(), log);
    }
    for _ in 0..60 {
        let row: Option<(String, i64)> = sqlx::query_as(
            "SELECT chain_id, COUNT(*) FROM proxy_logs \
             WHERE tenant_id = $1 AND chain_id IS NOT NULL \
             GROUP BY chain_id LIMIT 1",
        )
        .bind(tenant)
        .fetch_optional(pool)
        .await
        .expect("query");
        if let Some((chain, count)) = row
            && count as usize >= n
        {
            return chain;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("the chain never reached {n} rows");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn every_release_field_and_the_deciding_model_is_tamper_evident(pool: PgPool) {
    // These six columns are inside the chain: outside it, an
    // edit to the recorded release, its carrier, the client label or the
    // model that decided was undetectable. Each edit — including NULLing the
    // value — must now break exactly the row it touched.
    let t = "t-av-v3-fields";
    common::seed_tenant(&pool, t).await;
    let chain = seed_release_chain(&pool, t, 3).await;

    let (seq, version): (i64, Option<String>) = sqlx::query_as(
        "SELECT chain_seq, chain_version FROM proxy_logs \
         WHERE chain_id = $1 ORDER BY chain_seq OFFSET 1 LIMIT 1",
    )
    .bind(&chain)
    .fetch_one(&pool)
    .await
    .expect("victim");
    assert_eq!(
        version.as_deref(),
        Some(audit_chain::CANONICAL_VERSION),
        "the writer records the format it sealed under"
    );

    let clean = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(clean.verdict, ChainVerdict::Unattested, "{clean:?}");

    for (column, edit) in [
        ("agent_impl_name", "'forged-agent'"),
        ("agent_impl_version", "'9.9.9'"),
        ("agent_impl_build", "'sha256-forged'"),
        ("agent_impl_source", "'token'"),
        ("client_label", "'browser'"),
        ("deciding_model", "'other-model'"),
        ("agent_impl_version", "NULL"),
        ("deciding_model", "NULL"),
    ] {
        let original: (Option<String>,) = sqlx::query_as(&format!(
            "SELECT {column} FROM proxy_logs WHERE chain_id = $1 AND chain_seq = $2"
        ))
        .bind(&chain)
        .bind(seq)
        .fetch_one(&pool)
        .await
        .expect("read");
        sqlx::query(&format!(
            "UPDATE proxy_logs SET {column} = {edit} WHERE chain_id = $1 AND chain_seq = $2"
        ))
        .bind(&chain)
        .bind(seq)
        .execute(&pool)
        .await
        .expect("tamper");

        let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
        assert_eq!(
            r.verdict,
            ChainVerdict::Broken,
            "{column} = {edit} went undetected"
        );
        assert_eq!(r.first_bad_seq, Some(seq), "{column}: wrong row named");

        sqlx::query(&format!(
            "UPDATE proxy_logs SET {column} = $3 WHERE chain_id = $1 AND chain_seq = $2"
        ))
        .bind(&chain)
        .bind(seq)
        .bind(original.0)
        .execute(&pool)
        .await
        .expect("restore");
        let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
        assert_eq!(
            r.verdict,
            ChainVerdict::Unattested,
            "{column}: restore did not heal"
        );
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn changing_a_rows_format_label_is_detected(pool: PgPool) {
    // chain_version is a label, not a lever: a row that names no format
    // does not verify, and one that names another cannot be stored.
    let t = "t-av-v3-label";
    common::seed_tenant(&pool, t).await;
    let chain = seed_release_chain(&pool, t, 3).await;
    for edit in ["'v4'", "'v3'", "'v2'", "'v1'"] {
        let refused = sqlx::query(&format!(
            "UPDATE proxy_logs SET chain_version = {edit} WHERE chain_id = $1 AND chain_seq = \
             (SELECT MAX(chain_seq) FROM proxy_logs WHERE chain_id = $1)"
        ))
        .bind(&chain)
        .execute(&pool)
        .await
        .expect_err("an older format label is refused by the database");
        assert_eq!(
            refused
                .as_database_error()
                .and_then(|e| e.code())
                .as_deref(),
            Some("23514"),
            "chain_version = {edit}: {refused}"
        );
    }
    {
        let edit = "NULL";
        sqlx::query(&format!(
            "UPDATE proxy_logs SET chain_version = {edit} WHERE chain_id = $1 AND chain_seq = \
             (SELECT MAX(chain_seq) FROM proxy_logs WHERE chain_id = $1)"
        ))
        .bind(&chain)
        .execute(&pool)
        .await
        .expect("relabel");
        let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
        assert_eq!(
            r.verdict,
            ChainVerdict::Broken,
            "chain_version = {edit} passed"
        );
    }
}

// ---------------------------------------------------------------------------
// Canonical format v4: the JSON decision records, through a real
// jsonb / json round trip.
// ---------------------------------------------------------------------------

/// Every v4 JSON column set, with keys out of order, nested objects, floats
/// and non-ASCII text: `jsonb` re-orders keys and re-spells numbers on the
/// way in, so an untouched chain verifying is itself the round-trip proof.
fn set_json_records(log: &mut brutor_gateway_core::models::db::ProxyLogInsert, i: usize) {
    log.compliance_tags = Some(serde_json::json!(["soc2.cc6.6", "eu_ai_act.high"]));
    log.guardrails_skipped = Some(serde_json::json!([
        {"reason": "detector_timeout", "config": "gr-2", "check": "toxic_content"}
    ]));
    log.guardrails_degraded = Some(serde_json::json!([
        {"tier": "fallback", "config": "gr-1", "check": "pii"}
    ]));
    log.pre_action_state = Some(serde_json::json!({
        "scope": "tenant", "kind": "llm_quota", "used_tokens": 1200 + i,
        "spend_usd": 41.5, "ratio": 0.1234, "big": 1e21, "tiny": 1e-7,
        "flags": {"z": true, "a": null}
    }));
    log.custom_metadata = Some(serde_json::json!({
        "otel": {"span_name": "tool \u{20ac}", "service_name": "crm"},
        "conversation_handle": "01K6V4HANDLE0000000000000",
        "\u{1f600}": 1, "\u{fb33}": 2
    }));
    log.tags = Some(serde_json::json!({"refusal": "quota", "limits": [1, 2.5]}));
}

/// `(column, SQL type)` for every JSON column v4 covers.
const V4_JSON_COLUMNS: [(&str, &str); 6] = [
    ("compliance_tags", "jsonb"),
    ("guardrails_skipped", "jsonb"),
    ("guardrails_degraded", "jsonb"),
    ("pre_action_state", "jsonb"),
    ("custom_metadata", "json"),
    ("tags", "json"),
];

/// The same JSON data laid out differently: keys reversed, whitespace
/// added, numbers re-spelt in exponent form.
fn relaid(v: &serde_json::Value) -> String {
    match v {
        serde_json::Value::Object(m) => {
            let mut keys: Vec<&String> = m.keys().collect();
            keys.reverse();
            let parts: Vec<String> = keys
                .iter()
                .map(|k| {
                    format!(
                        "{} :  {}",
                        serde_json::to_string(k).unwrap(),
                        relaid(&m[*k])
                    )
                })
                .collect();
            format!("{{ {} }}", parts.join(" ,\n "))
        }
        serde_json::Value::Array(a) => {
            let parts: Vec<String> = a.iter().map(relaid).collect();
            format!("[ {} ]", parts.join(" , "))
        }
        serde_json::Value::Number(n) => format!("{:E}", n.as_f64().unwrap()),
        other => other.to_string(),
    }
}

async fn set_json_column(
    pool: &PgPool,
    chain: &str,
    seq: i64,
    column: &str,
    ty: &str,
    value: &str,
) {
    sqlx::query(&format!(
        "UPDATE proxy_logs SET {column} = $3::text::{ty} WHERE chain_id = $1 AND chain_seq = $2"
    ))
    .bind(chain)
    .bind(seq)
    .bind(value)
    .execute(pool)
    .await
    .expect("update");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn every_json_record_is_tamper_evident_and_survives_the_database_round_trip(pool: PgPool) {
    // What format v4 covers: the JSON decision records (checks skipped or
    // degraded, the state a quota decision was taken against, the
    // compliance tags) sat outside the chain, so rewriting "this call was
    // never checked" into "checked and clean" left the chain verifying.
    let t = "t-av-v4-json";
    common::seed_tenant(&pool, t).await;
    let chain = seed_release_chain(&pool, t, 3).await;

    // The schema really is what this test assumes: jsonb re-orders, json
    // keeps the text — both must verify.
    for (column, ty) in V4_JSON_COLUMNS {
        let (actual,): (String,) = sqlx::query_as(
            "SELECT data_type FROM information_schema.columns \
             WHERE table_name = 'proxy_logs' AND column_name = $1",
        )
        .bind(column)
        .fetch_one(&pool)
        .await
        .expect("column type");
        assert_eq!(actual, ty, "{column}");
    }

    let clean = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(
        clean.verdict,
        ChainVerdict::Unattested,
        "an honest v4 row read back through jsonb/json must verify: {clean:?}"
    );

    let (seq,): (i64,) = sqlx::query_as(
        "SELECT chain_seq FROM proxy_logs WHERE chain_id = $1 ORDER BY chain_seq OFFSET 1 LIMIT 1",
    )
    .bind(&chain)
    .fetch_one(&pool)
    .await
    .expect("victim");

    for (column, ty) in V4_JSON_COLUMNS {
        let (original,): (Option<String>,) = sqlx::query_as(&format!(
            "SELECT {column}::text FROM proxy_logs WHERE chain_id = $1 AND chain_seq = $2"
        ))
        .bind(&chain)
        .bind(seq)
        .fetch_one(&pool)
        .await
        .expect("read");
        let original = original.expect("seeded");

        // The same data, re-ordered and re-spelt: not a tamper.
        let parsed: serde_json::Value = serde_json::from_str(&original).unwrap();
        set_json_column(&pool, &chain, seq, column, ty, &relaid(&parsed)).await;
        let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
        assert_eq!(
            r.verdict,
            ChainVerdict::Unattested,
            "{column}: re-laid JSON read as a tamper: {r:?}"
        );

        // A value change is a tamper, on exactly this row.
        for forged in [r#"{"forged":true}"#, "[]"] {
            set_json_column(&pool, &chain, seq, column, ty, forged).await;
            let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
            assert_eq!(
                r.verdict,
                ChainVerdict::Broken,
                "{column} = {forged} went undetected"
            );
            assert_eq!(r.first_bad_seq, Some(seq), "{column}: wrong row named");
        }
        // Removing the document — SQL NULL or a JSON null — is one too.
        for removed in ["NULL", "'null'"] {
            sqlx::query(&format!(
                "UPDATE proxy_logs SET {column} = {removed} WHERE chain_id = $1 AND chain_seq = $2"
            ))
            .bind(&chain)
            .bind(seq)
            .execute(&pool)
            .await
            .expect("remove");
            let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
            assert_eq!(
                r.verdict,
                ChainVerdict::Broken,
                "{column} = {removed} went undetected"
            );
        }

        set_json_column(&pool, &chain, seq, column, ty, &original).await;
        let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
        assert_eq!(
            r.verdict,
            ChainVerdict::Unattested,
            "{column}: restore did not heal"
        );
    }

    // A small edit inside a document — one number — is caught too.
    sqlx::query(
        "UPDATE proxy_logs \
            SET pre_action_state = jsonb_set(pre_action_state, '{spend_usd}', '41.51') \
          WHERE chain_id = $1 AND chain_seq = $2",
    )
    .bind(&chain)
    .bind(seq)
    .execute(&pool)
    .await
    .expect("nudge");
    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(r.verdict, ChainVerdict::Broken);
    assert_eq!(r.first_bad_seq, Some(seq));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_nanosecond_clock_read_still_verifies_after_the_database_round_trip(pool: PgPool) {
    // The Linux CI failure (every untouched chain read `broken`): the writer
    // hashed `crate::common::db_now()` with nanoseconds, Postgres stored microseconds,
    // and the verifier recomputed from what was stored. macOS clocks tick in
    // microseconds, which is why it only ever failed on Linux. Pinned here
    // with an explicit nanosecond timestamp so it fails on every platform.
    use chrono::Timelike;
    let t = "t-av-nanos";
    common::seed_tenant(&pool, t).await;
    let writer = BackgroundWriter::new();
    for _ in 0..3 {
        let auth = test_auth(t);
        let mut log = proxy_logger::build_proxy_log(
            "llm",
            &auth,
            "POST",
            "/v1/chat/completions",
            None,
            None,
            200,
            3.0,
            true,
            None,
            &format!("corr-{}", Ulid::new()),
        );
        log.timestamp = log.timestamp.with_nanosecond(123_456_789).unwrap();
        writer.write_proxy_log(pool.clone(), log);
    }
    let mut chain = None;
    for _ in 0..60 {
        let row: Option<(String, i64)> = sqlx::query_as(
            "SELECT chain_id, COUNT(*) FROM proxy_logs \
             WHERE tenant_id = $1 AND chain_id IS NOT NULL GROUP BY chain_id LIMIT 1",
        )
        .bind(t)
        .fetch_optional(&pool)
        .await
        .expect("query");
        if let Some((c, n)) = row
            && n >= 3
        {
            chain = Some(c);
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let chain = chain.expect("rows landed");
    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(
        r.first_bad_seq, None,
        "an honest row read back from Postgres must verify"
    );
    assert_eq!(r.verdict, ChainVerdict::Unattested);
}

/// The row hash covers `custom_metadata` as RFC 8785
/// text, which prints a number as the double it denotes: 2^53+1 and 2^53 are
/// the same text. A row that carried such an integer could be edited by one
/// and still verify. The writer stores it as its exact digits (a string), so
/// what the database holds is what the hash covers, the row is not lost, and
/// the edit is a broken chain.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_integer_beyond_2_pow_53_in_a_sealed_column_cannot_be_nudged(pool: PgPool) {
    let t = "t-av-f1218";
    common::seed_tenant(&pool, t).await;
    let writer = BackgroundWriter::new();
    let mut log = proxy_logger::build_proxy_log(
        "llm",
        &test_auth(t),
        "POST",
        "/v1/chat/completions",
        None,
        None,
        200,
        3.0,
        true,
        None,
        &format!("corr-{}", Ulid::new()),
    );
    log.custom_metadata = Some(serde_json::json!({
        "amount_minor": 9_007_199_254_740_993_u64,
        "safe": 9_007_199_254_740_991_u64,
        "ratio": 0.25,
    }));
    let id = log.id.clone();
    writer.write_proxy_log(pool.clone(), log);

    let mut stored: Option<(String, serde_json::Value)> = None;
    for _ in 0..40 {
        stored = sqlx::query_as(
            "SELECT chain_id, custom_metadata::jsonb FROM proxy_logs \
             WHERE id = $1 AND chain_id IS NOT NULL",
        )
        .bind(&id)
        .fetch_optional(&pool)
        .await
        .expect("query");
        if stored.is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let (chain, meta) = stored.expect("the row is written, not lost");
    assert_eq!(
        meta,
        serde_json::json!({
            "amount_minor": "9007199254740993",
            "safe": 9_007_199_254_740_991_u64,
            "ratio": 0.25,
        }),
        "the unsafe integer is stored as its digits; everything else as written"
    );

    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(r.first_bad_seq, None, "the stored row verifies");
    assert_eq!(r.verdict, ChainVerdict::Unattested);

    // The edit the finding describes: one less, the same double.
    sqlx::query(
        "UPDATE proxy_logs SET custom_metadata = \
         replace(custom_metadata::text, '9007199254740993', '9007199254740992')::json \
         WHERE id = $1",
    )
    .bind(&id)
    .execute(&pool)
    .await
    .expect("tamper");
    let r = verify_chain(&pool, &chain, 0, 100).await.expect("verify");
    assert_eq!(r.verdict, ChainVerdict::Broken, "the edit went undetected");
}

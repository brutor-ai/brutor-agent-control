// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Witness registration against a mock witness: what a
//! receipt must prove before a head's grade rises, and that every way a
//! witness can fail leaves the head no better than it was.
//!
//! Grades never go up on a witness's say-so. Only a receipt that verifies
//! under the key pinned here counts; a key the witness presents is display
//! only.

use base64::Engine as _;
use brutor_cose::merkle::{inclusion_path, leaf_hash, tree_hash};
use brutor_cose::receipt::{InclusionProof, build_receipt};
use brutor_cose::sign1::{CoseSign1, sign_ed25519};
use brutor_gateway_core::services::evidence::witness;
use ed25519_dalek::SigningKey;
use serde_json::json;
use sqlx::PgPool;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "t-witness";
const OTHER_T: &str = "t-witness-other";
const HEAD: &str = "head-1";

static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

fn switch_on() {
    // The kill switch is process-wide; SERIAL keeps this module's tests from
    // overlapping (nextest gives each its own process anyway).
    unsafe {
        std::env::remove_var("BRUTOR_EVIDENCE_WITNESS_ENABLED");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
}

fn head_key() -> SigningKey {
    SigningKey::from_bytes(&[3u8; 32])
}

fn witness_key(n: u8) -> SigningKey {
    SigningKey::from_bytes(&[n; 32])
}

fn statement() -> Vec<u8> {
    sign_ed25519(vec![], vec![], Some(b"a tree head"), &head_key())
        .unwrap()
        .encode()
        .unwrap()
}

/// A SCITT receipt over `stmt`, signed by `key`.
fn receipt(stmt: &[u8], key: &SigningKey) -> Vec<u8> {
    let leaves = vec![leaf_hash(b"x"), leaf_hash(stmt), leaf_hash(b"y")];
    let proof = InclusionProof {
        tree_size: 3,
        leaf_index: 1,
        path: inclusion_path(1, &leaves),
    };
    build_receipt(&proof, &tree_hash(&leaves), key, Some(1_800_000_000)).unwrap()
}

fn b64(b: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(b)
}

fn pin(key: &SigningKey) -> String {
    hex::encode(key.verifying_key().to_bytes())
}

async fn seed_head(pool: &PgPool, id: &str, tenant: &str, stmt: &[u8]) {
    sqlx::query(
        "INSERT INTO evidence_tree_heads (id, log_id, tenant_id, tree_size, root, statement, \
         issued_at) VALUES ($1, $1, $2, 1, 'root', $3, now())",
    )
    .bind(id)
    .bind(tenant)
    .bind(stmt)
    .execute(pool)
    .await
    .unwrap();
}

type ReceiptRow = (
    String,
    Option<i64>,
    Option<i64>,
    Option<i64>,
    Option<chrono::DateTime<chrono::Utc>>,
);

struct W<'a> {
    id: &'a str,
    scope: &'a str,
    tenant: Option<&'a str>,
    kind: &'a str,
    url: String,
    pinned: Option<String>,
    enabled: bool,
    same_operator: bool,
}

impl<'a> W<'a> {
    fn new(id: &'a str, url: &str, pinned: Option<String>) -> Self {
        Self {
            id,
            scope: "platform",
            tenant: None,
            kind: "scitt",
            url: url.to_string(),
            pinned,
            enabled: true,
            same_operator: false,
        }
    }
}

async fn seed_witness(pool: &PgPool, w: W<'_>) {
    sqlx::query(
        "INSERT INTO witnesses (id, scope, tenant_id, kind, url, operator_label, pinned_key, \
         enabled, same_operator) VALUES ($1, $2, $3, $4, $5, 'op', $6, $7, $8)",
    )
    .bind(w.id)
    .bind(w.scope)
    .bind(w.tenant)
    .bind(w.kind)
    .bind(w.url)
    .bind(w.pinned)
    .bind(w.enabled)
    .bind(w.same_operator)
    .execute(pool)
    .await
    .unwrap();
}

async fn scitt_ok(body_receipt: Vec<u8>) -> MockServer {
    let m = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/transparency/register-statement"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "receipt_b64": b64(&body_receipt),
        })))
        .mount(&m)
        .await;
    m
}

async fn grade(pool: &PgPool, head: &str) -> (String, Option<Vec<u8>>) {
    sqlx::query_as("SELECT grade, transparent_statement FROM evidence_tree_heads WHERE id = $1")
        .bind(head)
        .fetch_one(pool)
        .await
        .unwrap()
}

async fn verdicts(pool: &PgPool, witness_id: &str) -> Vec<(String, Option<String>)> {
    sqlx::query_as(
        "SELECT verdict, error FROM evidence_receipts WHERE witness_id = $1 ORDER BY created_at",
    )
    .bind(witness_id)
    .fetch_all(pool)
    .await
    .unwrap()
}

async fn setup(pool: &PgPool) -> Vec<u8> {
    common::seed_tenant(pool, T).await;
    common::seed_tenant(pool, OTHER_T).await;
    let s = statement();
    seed_head(pool, HEAD, T, &s).await;
    s
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_verified_receipt_from_an_independent_witness_raises_the_grade(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    let s = setup(&pool).await;
    let key = witness_key(7);
    let m = scitt_ok(receipt(&s, &key)).await;
    seed_witness(&pool, W::new("w1", &m.uri(), Some(pin(&key)))).await;

    witness::register_head(pool.clone(), HEAD.into()).await;

    let (g, transparent) = grade(&pool, HEAD).await;
    assert_eq!(g, "witnessed");
    let row: ReceiptRow = sqlx::query_as(
        "SELECT verdict, leaf_index, tree_size, witness_iat, verified_at FROM evidence_receipts",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    let (verdict, leaf, size, iat, verified_at) = row;
    assert_eq!(verdict, "witnessed");
    assert_eq!((leaf, size, iat), (Some(1), Some(3), Some(1_800_000_000)));
    assert!(verified_at.is_some());

    // The receipt travels at label 394, and the head's own signature still holds.
    let t = CoseSign1::decode(&transparent.expect("transparent statement")).unwrap();
    assert_eq!(t.receipts().len(), 1);
    t.verify_ed25519(&head_key().verifying_key(), None).unwrap();

    // What the witness received is the statement, base64 in JSON.
    let sent = &m.received_requests().await.unwrap()[0];
    let body: serde_json::Value = serde_json::from_slice(&sent.body).unwrap();
    assert_eq!(body["signed_statement_b64"], b64(&s));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn two_independent_witnesses_make_a_plural_head_and_one_operator_counts_once(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    let s = setup(&pool).await;
    let (k1, k2) = (witness_key(7), witness_key(8));
    let (m1, m2) = (
        scitt_ok(receipt(&s, &k1)).await,
        scitt_ok(receipt(&s, &k2)).await,
    );
    seed_witness(&pool, W::new("w1", &m1.uri(), Some(pin(&k1)))).await;
    seed_witness(&pool, W::new("w2", &m2.uri(), Some(pin(&k2)))).await;
    witness::register_head(pool.clone(), HEAD.into()).await;
    let (g, transparent) = grade(&pool, HEAD).await;
    assert_eq!(g, "plural");
    assert_eq!(
        CoseSign1::decode(&transparent.unwrap())
            .unwrap()
            .receipts()
            .len(),
        2
    );
}

/// A same-operator witness is not independent, however good its receipt.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_same_operator_witness_only_self_witnesses(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    let s = setup(&pool).await;
    let key = witness_key(7);
    let m = scitt_ok(receipt(&s, &key)).await;
    let mut w = W::new("w1", &m.uri(), Some(pin(&key)));
    w.same_operator = true;
    seed_witness(&pool, w).await;
    witness::register_head(pool.clone(), HEAD.into()).await;
    assert_eq!(grade(&pool, HEAD).await.0, "self_witnessed");
}

/// The pin is the trust anchor: a receipt signed by anyone else proves nothing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_receipt_under_another_key_is_invalid_and_grants_nothing(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    let s = setup(&pool).await;
    let m = scitt_ok(receipt(&s, &witness_key(9))).await;
    seed_witness(&pool, W::new("w1", &m.uri(), Some(pin(&witness_key(7))))).await;
    witness::register_head(pool.clone(), HEAD.into()).await;
    let (g, transparent) = grade(&pool, HEAD).await;
    assert_eq!(g, "standalone");
    assert!(transparent.is_none(), "an invalid receipt is not attached");
    assert_eq!(verdicts(&pool, "w1").await[0].0, "invalid");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_receipt_over_another_statement_is_invalid(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    setup(&pool).await;
    let key = witness_key(7);
    let m = scitt_ok(receipt(b"some other statement", &key)).await;
    seed_witness(&pool, W::new("w1", &m.uri(), Some(pin(&key)))).await;
    witness::register_head(pool.clone(), HEAD.into()).await;
    assert_eq!(grade(&pool, HEAD).await.0, "standalone");
    assert_eq!(verdicts(&pool, "w1").await[0].0, "invalid");
}

/// No pin, no trust: not evidence of forgery either.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unpinned_witness_stays_unverified(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    let s = setup(&pool).await;
    let m = scitt_ok(receipt(&s, &witness_key(7))).await;
    seed_witness(&pool, W::new("w1", &m.uri(), None)).await;
    witness::register_head(pool.clone(), HEAD.into()).await;
    assert_eq!(grade(&pool, HEAD).await.0, "standalone");
    assert_eq!(verdicts(&pool, "w1").await[0].0, "unverified");
}

/// Every way a witness can fail: the head stays where it was, and the failure
/// is recorded for the operator.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_failing_witness_leaves_the_head_standalone_and_says_why(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    let cases: Vec<(&str, ResponseTemplate, &str)> = vec![
        (
            "w-500",
            ResponseTemplate::new(503).set_body_string("down"),
            "HTTP 503",
        ),
        (
            "w-notjson",
            ResponseTemplate::new(200).set_body_string("nope"),
            "registration failed",
        ),
        (
            "w-noreceipt",
            ResponseTemplate::new(200).set_body_json(json!({})),
            "no receipt_b64",
        ),
        (
            "w-badb64",
            ResponseTemplate::new(200).set_body_json(json!({"receipt_b64": "!!!"})),
            "receipt_b64",
        ),
    ];
    setup(&pool).await;
    let mut mocks = Vec::new();
    for (id, tmpl, _) in &cases {
        let m = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(tmpl.clone())
            .mount(&m)
            .await;
        seed_witness(&pool, W::new(id, &m.uri(), Some(pin(&witness_key(7))))).await;
        mocks.push(m);
    }
    seed_witness(
        &pool,
        W::new(
            "w-denied",
            "http://169.254.169.254/",
            Some(pin(&witness_key(7))),
        ),
    )
    .await;
    seed_witness(&pool, W::new("w-down", "http://127.0.0.1:1/", None)).await;
    let mut unknown = W::new("w-kind", "https://x.invalid/", None);
    unknown.kind = "weird";
    seed_witness(&pool, unknown).await;

    witness::register_head(pool.clone(), HEAD.into()).await;

    assert_eq!(grade(&pool, HEAD).await.0, "standalone");
    for (id, _, expect) in &cases {
        let v = verdicts(&pool, id).await;
        assert_eq!(v.len(), 1, "{id}");
        assert_eq!(v[0].0, "invalid", "{id}");
        let err = v[0].1.as_deref().unwrap();
        assert!(err.contains(expect), "{id}: {err}");
    }
    assert!(
        verdicts(&pool, "w-denied").await[0]
            .1
            .as_deref()
            .unwrap()
            .contains("outbound policy")
    );
    assert_eq!(verdicts(&pool, "w-down").await[0].0, "invalid");
    assert!(
        verdicts(&pool, "w-kind").await[0]
            .1
            .as_deref()
            .unwrap()
            .contains("not supported")
    );
}

/// One bad witness does not cost the head the good one.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_failing_witness_does_not_stop_a_good_one(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    let s = setup(&pool).await;
    let key = witness_key(7);
    let good = scitt_ok(receipt(&s, &key)).await;
    seed_witness(&pool, W::new("a-down", "http://127.0.0.1:1/", None)).await;
    seed_witness(&pool, W::new("b-good", &good.uri(), Some(pin(&key)))).await;
    witness::register_head(pool.clone(), HEAD.into()).await;
    assert_eq!(grade(&pool, HEAD).await.0, "witnessed");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn registration_is_idempotent_per_head_and_witness(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    let s = setup(&pool).await;
    let key = witness_key(7);
    let m = scitt_ok(receipt(&s, &key)).await;
    seed_witness(&pool, W::new("w1", &m.uri(), Some(pin(&key)))).await;
    witness::register_head(pool.clone(), HEAD.into()).await;
    witness::register_head(pool.clone(), HEAD.into()).await;
    assert_eq!(m.received_requests().await.unwrap().len(), 1);
    assert_eq!(verdicts(&pool, "w1").await.len(), 1);
}

/// Unknown is not "not yet witnessed": a receipts table that cannot be read
/// means no second submission, not a duplicate one.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unreadable_receipts_table_submits_nothing(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    let s = setup(&pool).await;
    let key = witness_key(7);
    let m = scitt_ok(receipt(&s, &key)).await;
    seed_witness(&pool, W::new("w1", &m.uri(), Some(pin(&key)))).await;
    sqlx::query("ALTER TABLE evidence_receipts RENAME TO evidence_receipts_unreadable")
        .execute(&pool)
        .await
        .unwrap();
    witness::register_head(pool.clone(), HEAD.into()).await;
    assert_eq!(m.received_requests().await.unwrap().len(), 0);
    let g: String = sqlx::query_scalar("SELECT grade FROM evidence_tree_heads WHERE id = $1")
        .bind(HEAD)
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(g, "standalone");
}

/// A tenant's witness sees its own heads only, and a disabled one sees none.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_head_goes_only_to_enabled_witnesses_in_its_scope(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    let s = setup(&pool).await;
    let key = witness_key(7);
    let (mine, theirs, off, platform) = (
        scitt_ok(receipt(&s, &key)).await,
        scitt_ok(receipt(&s, &key)).await,
        scitt_ok(receipt(&s, &key)).await,
        scitt_ok(receipt(&s, &key)).await,
    );
    let mut w = W::new("w-mine", &mine.uri(), Some(pin(&key)));
    (w.scope, w.tenant) = ("tenant", Some(T));
    seed_witness(&pool, w).await;
    let mut w = W::new("w-theirs", &theirs.uri(), Some(pin(&key)));
    (w.scope, w.tenant) = ("tenant", Some(OTHER_T));
    seed_witness(&pool, w).await;
    let mut w = W::new("w-off", &off.uri(), Some(pin(&key)));
    w.enabled = false;
    seed_witness(&pool, w).await;
    seed_witness(
        &pool,
        W::new("w-platform", &platform.uri(), Some(pin(&key))),
    )
    .await;

    witness::register_head(pool.clone(), HEAD.into()).await;

    assert_eq!(mine.received_requests().await.unwrap().len(), 1);
    assert_eq!(platform.received_requests().await.unwrap().len(), 1);
    assert_eq!(theirs.received_requests().await.unwrap().len(), 0);
    assert_eq!(off.received_requests().await.unwrap().len(), 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_kill_switch_stops_all_egress(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    assert!(witness::egress_enabled(), "on by default");
    let s = setup(&pool).await;
    let key = witness_key(7);
    let m = scitt_ok(receipt(&s, &key)).await;
    seed_witness(&pool, W::new("w1", &m.uri(), Some(pin(&key)))).await;
    for off in ["off", "0", "false", " NO "] {
        unsafe {
            std::env::set_var("BRUTOR_EVIDENCE_WITNESS_ENABLED", off);
        }
        brutor_gateway_core::env_vars::reload_for_tests();
        assert!(!witness::egress_enabled(), "{off}");
        witness::register_head(pool.clone(), HEAD.into()).await;
        witness::retry_pending(&pool).await;
    }
    assert_eq!(m.received_requests().await.unwrap().len(), 0);
    unsafe {
        std::env::set_var("BRUTOR_EVIDENCE_WITNESS_ENABLED", "on");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
    assert!(witness::egress_enabled());
    switch_on();
}

/// With the kill switch off a head is not merely left alone: the evidence
/// says, per witness, that it was not submitted and why, once, and the head
/// stays unwitnessed. Switched on again, the retry loop submits it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_kill_switch_records_the_head_as_unwitnessed_and_says_why(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    let s = setup(&pool).await;
    let key = witness_key(7);
    let m = scitt_ok(receipt(&s, &key)).await;
    seed_witness(&pool, W::new("w1", &m.uri(), Some(pin(&key)))).await;
    unsafe {
        std::env::set_var("BRUTOR_EVIDENCE_WITNESS_ENABLED", "off");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
    witness::register_head(pool.clone(), HEAD.into()).await;
    witness::register_head(pool.clone(), HEAD.into()).await;

    assert_eq!(m.received_requests().await.unwrap().len(), 0);
    assert_eq!(grade(&pool, HEAD).await.0, "standalone");
    let v = verdicts(&pool, "w1").await;
    assert_eq!(v.len(), 1, "recorded once: {v:?}");
    assert_eq!(v[0].0, witness::VERDICT_SKIPPED);
    let why = v[0].1.as_deref().unwrap_or_default();
    assert!(
        why.contains("BRUTOR_EVIDENCE_WITNESS_ENABLED") && why.contains("unwitnessed"),
        "{why}"
    );

    switch_on();
    witness::retry_pending(&pool).await;
    assert_eq!(m.received_requests().await.unwrap().len(), 1);
    assert_eq!(grade(&pool, HEAD).await.0, "witnessed");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_head_that_does_not_exist_or_has_no_witness_is_a_no_op(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    setup(&pool).await;
    witness::register_head(pool.clone(), "no-such-head".into()).await;
    witness::register_head(pool.clone(), HEAD.into()).await;
    assert_eq!(grade(&pool, HEAD).await.0, "standalone");
    let n: i64 = sqlx::query_scalar("SELECT count(*) FROM evidence_receipts")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(n, 0);
}

// ── RFC 3161 ───────────────────────────────────────────────────────────

fn tsr() -> (Vec<u8>, Vec<u8>, String) {
    let dir = crate::common::community_root()
        .join("tests/fixtures/rfc3161")
        .display()
        .to_string();
    (
        std::fs::read(format!("{dir}/ecp256.tsr")).unwrap(),
        std::fs::read(format!("{dir}/statement.bin")).unwrap(),
        std::fs::read_to_string(format!("{dir}/ecp256.crt.sha256")).unwrap(),
    )
}

/// RFC 3161 §2.4.2 — a reply that does not echo this request's nonce is a
/// replay, even a perfectly signed one.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tsa_reply_with_another_nonce_is_refused(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    common::seed_tenant(&pool, T).await;
    let (token, stmt, fp) = tsr();
    seed_head(&pool, HEAD, T, &stmt).await;

    // The fixture is a real TimeStampResp, for some earlier request's nonce.
    let m = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_bytes(token))
        .mount(&m)
        .await;
    let mut w = W::new("w-tsa", &m.uri(), Some(fp.trim().to_string()));
    w.kind = "rfc3161";
    seed_witness(&pool, w).await;

    witness::register_head(pool.clone(), HEAD.into()).await;

    let v = verdicts(&pool, "w-tsa").await;
    assert_eq!(v[0].0, "invalid");
    assert!(v[0].1.as_deref().unwrap().contains("nonce"), "{v:?}");
    assert_eq!(grade(&pool, HEAD).await.0, "standalone");

    let sent = &m.received_requests().await.unwrap()[0];
    assert_eq!(
        sent.headers.get("content-type").unwrap().to_str().unwrap(),
        brutor_tsp::REQUEST_CONTENT_TYPE
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tsa_error_or_garbage_is_a_failed_registration(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    common::seed_tenant(&pool, T).await;
    let (_, stmt, _) = tsr();
    seed_head(&pool, HEAD, T, &stmt).await;
    let cases = [
        ("w-tsa-500", ResponseTemplate::new(500), "HTTP 500"),
        (
            "w-tsa-junk",
            ResponseTemplate::new(200).set_body_bytes(b"junk".to_vec()),
            "registration failed",
        ),
    ];
    let mut mocks = Vec::new();
    for (id, tmpl, _) in &cases {
        let m = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(tmpl.clone())
            .mount(&m)
            .await;
        let mut w = W::new(id, &m.uri(), None);
        w.kind = "rfc3161";
        seed_witness(&pool, w).await;
        mocks.push(m);
    }
    let mut w = W::new("w-tsa-denied", "http://169.254.169.254/", None);
    w.kind = "rfc3161";
    seed_witness(&pool, w).await;

    witness::register_head(pool.clone(), HEAD.into()).await;

    for (id, _, expect) in &cases {
        let v = verdicts(&pool, id).await;
        assert_eq!(v[0].0, "invalid", "{id}");
        assert!(v[0].1.as_deref().unwrap().contains(expect), "{id}: {v:?}");
    }
    assert!(
        verdicts(&pool, "w-tsa-denied").await[0]
            .1
            .as_deref()
            .unwrap()
            .contains("outbound policy")
    );
}

/// A timestamp proves when, not that anyone else holds the head: it never
/// raises the grade and is not carried inside the statement.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_timestamp_does_not_raise_the_grade(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    let (token, stmt, _) = tsr();
    seed_head(&pool, HEAD, T, &stmt).await;
    seed_witness(
        &pool,
        W {
            kind: "rfc3161",
            ..W::new("w-tsa", "https://tsa.invalid/", None)
        },
    )
    .await;
    sqlx::query(
        "INSERT INTO evidence_receipts (id, tree_head_id, witness_id, kind, receipt, verdict) \
         VALUES ('rcp-1', $1, 'w-tsa', 'rfc3161', $2, 'timestamped')",
    )
    .bind(HEAD)
    .bind(&token)
    .execute(&pool)
    .await
    .unwrap();
    witness::regrade(&pool, HEAD, &stmt).await.unwrap();
    let (g, transparent) = grade(&pool, HEAD).await;
    assert_eq!(g, "standalone");
    assert!(transparent.is_none());
}

/// A receipt with no surviving witness row counts as same-operator: nothing a
/// deleted witness said can make a head look independently witnessed.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_receipt_whose_witness_is_gone_is_not_independent(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    let s = statement();
    seed_head(&pool, HEAD, T, &s).await;
    sqlx::query(
        "INSERT INTO evidence_receipts (id, tree_head_id, witness_id, kind, receipt, verdict) \
         VALUES ('rcp-2', $1, NULL, 'scitt', $2, 'witnessed')",
    )
    .bind(HEAD)
    .bind(receipt(&s, &witness_key(7)))
    .execute(&pool)
    .await
    .unwrap();
    witness::regrade(&pool, HEAD, &s).await.unwrap();
    assert_eq!(grade(&pool, HEAD).await.0, "self_witnessed");
}

/// Not a COSE statement: the grade is still computed; there is nothing to
/// attach the receipt to.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_statement_that_is_not_cose_gets_no_transparent_form(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    seed_head(&pool, HEAD, T, b"not cose").await;
    let key = witness_key(7);
    seed_witness(&pool, W::new("w1", "https://w.invalid/", Some(pin(&key)))).await;
    sqlx::query(
        "INSERT INTO evidence_receipts (id, tree_head_id, witness_id, kind, receipt, verdict) \
         VALUES ('rcp-3', $1, 'w1', 'scitt', $2, 'witnessed')",
    )
    .bind(HEAD)
    .bind(receipt(b"not cose", &key))
    .execute(&pool)
    .await
    .unwrap();
    witness::regrade(&pool, HEAD, b"not cose").await.unwrap();
    let (g, transparent) = grade(&pool, HEAD).await;
    assert_eq!(g, "witnessed");
    assert!(transparent.is_none());
}

// ── Retry ──────────────────────────────────────────────────────────────

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_retry_redrives_unwitnessed_heads_only(pool: PgPool) {
    let _serial = SERIAL.lock().await;
    switch_on();
    let s = setup(&pool).await;
    let key = witness_key(7);

    // An old head is past the window and left alone.
    seed_head(&pool, "head-old", T, &s).await;
    sqlx::query(
        "UPDATE evidence_tree_heads SET issued_at = now() - interval '31 days' WHERE id = 'head-old'",
    )
    .execute(&pool)
    .await
    .unwrap();

    // The witness is down the first time, up the second.
    let m = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(503))
        .up_to_n_times(1)
        .mount(&m)
        .await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "receipt_b64": b64(&receipt(&s, &key)),
            "leaf_index": 11,
            "tree_size": 12,
        })))
        .mount(&m)
        .await;
    seed_witness(&pool, W::new("w1", &m.uri(), Some(pin(&key)))).await;

    witness::retry_pending(&pool).await;
    assert_eq!(grade(&pool, HEAD).await.0, "standalone", "still down");
    witness::retry_pending(&pool).await;
    assert_eq!(grade(&pool, HEAD).await.0, "witnessed", "back up");
    assert_eq!(grade(&pool, "head-old").await.0, "standalone");

    // Witnessed now: a further pass sends nothing.
    let before = m.received_requests().await.unwrap().len();
    witness::retry_pending(&pool).await;
    assert_eq!(m.received_requests().await.unwrap().len(), before);

    // What the witness reported about its log wins over what the receipt says.
    let (leaf, size): (Option<i64>, Option<i64>) = sqlx::query_as(
        "SELECT leaf_index, tree_size FROM evidence_receipts WHERE verdict = 'witnessed'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!((leaf, size), (Some(11), Some(12)));
}

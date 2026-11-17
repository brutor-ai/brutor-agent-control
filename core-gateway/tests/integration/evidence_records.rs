// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The writer mints a sealed action record for every
//! consequential row and every refusal, appends it to the tenant's
//! RFC 6962 log, issues signed tree heads on cadence, and the whole thing
//! verifies offline from what landed in the database.

use std::time::Duration;

use brutor_cose::merkle::{leaf_hash, verify_inclusion};
use brutor_ctlog::{Log, MemoryStore, NodeStore, TreeHead};
use brutor_evidence::statement::verify_record_statement;
use brutor_gateway_core::models::db::ProxyLogInsert;
use brutor_gateway_core::services::background_writer::BackgroundWriter;
use brutor_gateway_core::services::evidence::key;
use brutor_gateway_core::services::proxy_logger;
use ed25519_dalek::SigningKey;
use sqlx::PgPool;
use ulid::Ulid;

use crate::background_writer_chain::test_auth;
use crate::common;

const SEED: [u8; 32] = [0x42; 32];

fn enable_evidence() {
    // Process-global by nature; the key cache re-reads the environment so
    // this is safe to call from any test in any order.
    unsafe {
        std::env::set_var("BRUTOR_EVIDENCE_SIGNING_KEY", hex::encode(SEED));
        std::env::set_var("BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES", "2");
        std::env::set_var("BRUTOR_EVIDENCE_WITNESS_ENABLED", "off");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
}

fn signing() -> SigningKey {
    SigningKey::from_bytes(&SEED)
}

fn mcp_row(tenant: &str, method: &str, success: bool, status: u16) -> ProxyLogInsert {
    let auth = test_auth(tenant);
    let mut log = proxy_logger::build_proxy_log(
        "mcp",
        &auth,
        "POST",
        "/v1/proxy/mcp/srv-1",
        None,
        Some("srv-1"),
        status,
        9.0,
        success,
        if success { None } else { Some("refused") },
        &format!("corr-{}", Ulid::new()),
    );
    log.mcp_method = Some(method.to_string());
    log.mcp_capability_name = Some("write_order".into());
    log.request_body_preview = Some(r#"{"order_id":"PO-1","total":"12.50"}"#.into());
    log.response_body_preview = Some(r#"{"ok":true}"#.into());
    log.root_task_id = Some(format!("run-{}", Ulid::new()));
    log.subject_id = Some("end-user-7".into());
    log
}

async fn write_rows(pool: &PgPool, rows: Vec<ProxyLogInsert>) {
    let ids: Vec<String> = rows.iter().map(|r| r.id.clone()).collect();
    let writer = BackgroundWriter::new();
    for r in rows {
        writer.write_proxy_log(pool.clone(), r);
    }
    for _ in 0..80 {
        let (n,): (i64,) = sqlx::query_as("SELECT count(*) FROM proxy_logs WHERE id = ANY($1)")
            .bind(&ids)
            .fetch_one(pool)
            .await
            .unwrap();
        if n as usize == ids.len() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("background writer never persisted all rows");
}

async fn load_store(pool: &PgPool, log_id: &str) -> MemoryStore {
    let nodes: Vec<(i32, i64, String)> =
        sqlx::query_as("SELECT level, index, hash FROM evidence_log_nodes WHERE log_id = $1")
            .bind(log_id)
            .fetch_all(pool)
            .await
            .unwrap();
    let mut store = MemoryStore::default();
    for (level, index, hash) in nodes {
        let h: [u8; 32] = hex::decode(hash).unwrap().try_into().unwrap();
        store.put(level as u32, index as u64, h);
    }
    store
}

/// `(id, tree_size, root, prev_size, prev_root, statement, stamp_seq, grade)`
type HeadRow = (
    String,
    i64,
    String,
    i64,
    Option<String>,
    Vec<u8>,
    Option<i64>,
    String,
);

async fn load_heads(pool: &PgPool, log_id: &str) -> Vec<HeadRow> {
    sqlx::query_as(
        "SELECT id, tree_size, root, prev_size, prev_root, statement, stamp_seq, grade \
         FROM evidence_tree_heads WHERE log_id = $1 ORDER BY tree_size",
    )
    .bind(log_id)
    .fetch_all(pool)
    .await
    .unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn writer_seals_records_and_heads_that_verify_offline(pool: PgPool) {
    enable_evidence();
    let tenant = format!("tenant-ev-{}", Ulid::new()).to_lowercase();
    common::seed_tenant(&pool, &tenant).await;

    // A: executed tools/call. B: guardrail block (a refusal, no effect).
    // C: tools/list — a query, never sealed.
    let a = mcp_row(&tenant, "tools/call", true, 200);
    let mut b = mcp_row(&tenant, "tools/call", false, 403);
    b.guardrail_blocked = true;
    b.guardrail_check = Some("pii".into());
    b.guardrail_action = Some("block".into());
    let c = mcp_row(&tenant, "tools/list", true, 200);
    let (a_id, b_id, c_id) = (a.id.clone(), b.id.clone(), c.id.clone());
    write_rows(&pool, vec![a, b, c]).await;

    let rid = |id: &str| {
        let pool = pool.clone();
        let id = id.to_string();
        async move {
            let (r,): (Option<String>,) =
                sqlx::query_as("SELECT record_id FROM proxy_logs WHERE id = $1")
                    .bind(&id)
                    .fetch_one(&pool)
                    .await
                    .unwrap();
            r
        }
    };
    let a_rec = rid(&a_id).await.expect("executed call has a record");
    let b_rec = rid(&b_id).await.expect("guardrail block has a record");
    assert!(
        rid(&c_id).await.is_none(),
        "tools/list is a query and is not sealed"
    );

    // The records themselves.
    let rows: Vec<(String, i64, String, serde_json::Value, Vec<u8>, String)> = sqlx::query_as(
        "SELECT record_id, seq, verdict, record, statement, log_id FROM action_records \
         WHERE tenant_id = $1 ORDER BY seq",
    )
    .bind(&tenant)
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].0, a_rec);
    assert_eq!(rows[1].0, b_rec);
    assert_eq!(rows[0].2, "executed");
    assert_eq!(rows[1].2, "blocked");
    let log_id = rows[0].5.clone();
    let vk = signing().verifying_key();
    for (record_id, _seq, _verdict, record, statement, _) in &rows {
        let v = brutor_evidence::verify(record, None);
        assert!(v.ok, "{:?}", v.findings);
        assert_eq!(v.record_id.as_deref(), Some(record_id.as_str()));
        let info = verify_record_statement(statement, record, &vk).expect("statement verifies");
        assert_eq!(&info.record_id, record_id);
        assert_eq!(info.sub, format!("tenant:{tenant}"));
        // never-enters: nothing from the row's identity or bodies is on the record
        let dumped = record.to_string();
        assert!(!dumped.contains("end-user-7"));
        assert!(!dumped.contains("PO-1"));
        // the row hash is cited
        assert!(dumped.contains("ai.brutor.proxy-log-row"));
    }
    assert_eq!(rows[0].3["effect"]["status"], "confirmed");
    assert!(rows[1].3.get("effect").is_none());
    assert_eq!(rows[1].3["controls"][0]["id"], "ai.brutor.guardrail.pii");

    // Cadence 2 → a head was issued over the two records and stamped as leaf 2.
    let heads = load_heads(&pool, &log_id).await;
    assert_eq!(heads.len(), 1, "one head after the first batch");
    let (h1_id, h1_size, h1_root, h1_prev, h1_prev_root, h1_stmt, h1_stamp, h1_grade) = &heads[0];
    assert_eq!(*h1_size, 2);
    assert_eq!(*h1_prev, 0);
    assert!(h1_prev_root.is_none());
    assert_eq!(*h1_stamp, Some(2));
    assert_eq!(h1_grade, "standalone");
    let (head, _) = TreeHead::verify(h1_stmt, &vk).expect("tree head verifies under the key");
    assert_eq!(hex::encode(head.root), *h1_root);

    let (size, root, last_head): (i64, Option<String>, Option<String>) = sqlx::query_as(
        "SELECT tree_size, root, last_tree_head_id FROM evidence_logs WHERE log_id = $1",
    )
    .bind(&log_id)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(size, 3, "two records plus the head's stamp entry");
    assert_eq!(last_head.as_deref(), Some(h1_id.as_str()));

    // Inclusion proofs from the persisted nodes verify against the log root.
    let mut store = load_store(&pool, &log_id).await;
    let log = Log::open(&mut store, size as u64);
    let root_bytes: [u8; 32] = hex::decode(root.unwrap()).unwrap().try_into().unwrap();
    assert_eq!(log.root().unwrap(), root_bytes);
    for (_, seq, _, _, statement, _) in &rows {
        let path = log.inclusion_path(*seq as u64, size as u64).unwrap();
        verify_inclusion(
            &leaf_hash(statement),
            *seq as u64,
            size as u64,
            &path,
            &root_bytes,
        )
        .expect("record is in the log");
    }
    // The head's own stamp is a leaf too.
    let path = log.inclusion_path(2, size as u64).unwrap();
    verify_inclusion(&leaf_hash(h1_stmt), 2, size as u64, &path, &root_bytes).unwrap();

    // Second batch: the next head must extend the first (consistency proof
    // is checked inside TreeHead::verify).
    let d = mcp_row(&tenant, "tools/call", true, 200);
    let e = mcp_row(&tenant, "tools/call", true, 200);
    write_rows(&pool, vec![d, e]).await;
    let heads = load_heads(&pool, &log_id).await;
    assert_eq!(heads.len(), 2);
    let (_, h2_size, _h2_root, h2_prev, h2_prev_root, h2_stmt, _, _) = &heads[1];
    assert_eq!(*h2_size, 5, "3 + two new records");
    assert_eq!(*h2_prev, 2);
    assert_eq!(h2_prev_root.as_deref(), Some(h1_root.as_str()));
    let (h2, _) = TreeHead::verify(h2_stmt, &vk).expect("second head verifies incl. consistency");
    // RFC 6962 §2.1.4.1, 2 → 5: [MTH(D[2:4]), MTH(D[4:5])]
    assert_eq!(h2.consistency_proof.len(), 2);
    let (size2,): (i64,) = sqlx::query_as("SELECT tree_size FROM evidence_logs WHERE log_id = $1")
        .bind(&log_id)
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(size2, 6);
    // No witnesses configured → no receipts, grade stays standalone.
    let (receipts,): (i64,) = sqlx::query_as("SELECT count(*) FROM evidence_receipts")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(receipts, 0);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn tenants_get_separate_logs(pool: PgPool) {
    enable_evidence();
    let t1 = format!("tenant-ev-a-{}", Ulid::new()).to_lowercase();
    let t2 = format!("tenant-ev-b-{}", Ulid::new()).to_lowercase();
    common::seed_tenant(&pool, &t1).await;
    common::seed_tenant(&pool, &t2).await;
    write_rows(
        &pool,
        vec![
            mcp_row(&t1, "tools/call", true, 200),
            mcp_row(&t2, "tools/call", true, 200),
        ],
    )
    .await;
    let logs: Vec<(String, String, i64)> = sqlx::query_as(
        "SELECT log_id, tenant_id, tree_size FROM evidence_logs WHERE tenant_id = ANY($1)",
    )
    .bind(vec![t1.clone(), t2.clone()])
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(logs.len(), 2);
    assert_ne!(logs[0].0, logs[1].0);
    assert_eq!(logs[0].0, key::log_id_for(&logs[0].1));
    assert!(logs.iter().all(|l| l.2 >= 1));
}

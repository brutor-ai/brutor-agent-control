// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The evidence API the control plane calls: the seal
//! API, forced tree heads, the verify family, evidence bundles and
//! disclosure records, backfill, per-tenant key rotation and the row-chain
//! head records. Everything is driven through the HTTP routes with the
//! global service key, then checked against what landed in the database.
//!
//! The signing key, head cadence and witness kill switch are the same
//! process-wide values `evidence_records` sets (tests share a process); a
//! test that needs "evidence disabled" uses the task-local
//! `key::FORCE_DISABLED` instead of touching the environment.

use std::time::Duration;

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use base64::Engine as _;
use brutor_evidence::statement::verify_record_statement;
use brutor_gateway_core::middleware;
use brutor_gateway_core::models::db::ProxyLogInsert;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::background_writer::BackgroundWriter;
use brutor_gateway_core::services::evidence::key;
use brutor_gateway_core::services::proxy_logger;
use ed25519_dalek::{SigningKey, VerifyingKey};
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use ulid::Ulid;

use crate::background_writer_chain::test_auth;
use crate::common;

const SEED: [u8; 32] = [0x42; 32];

fn enable_evidence() {
    unsafe {
        std::env::set_var("BRUTOR_EVIDENCE_SIGNING_KEY", hex::encode(SEED));
        std::env::set_var("BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES", "2");
        std::env::set_var("BRUTOR_EVIDENCE_WITNESS_ENABLED", "off");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
}

fn deployment_key() -> SigningKey {
    SigningKey::from_bytes(&SEED)
}

fn b64(b: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(b)
}

fn unb64(s: &str) -> Vec<u8> {
    base64::engine::general_purpose::STANDARD.decode(s).unwrap()
}

struct Env {
    app: Router,
    service_key: String,
    tenant: String,
    #[allow(dead_code)]
    state: SharedState,
}

async fn setup(pool: &PgPool) -> Env {
    enable_evidence();
    let tenant = format!("tenant-evapi-{}", Ulid::new()).to_lowercase();
    common::seed_tenant(pool, &tenant).await;
    let rg = format!("rg-{}", Ulid::new()).to_lowercase();
    common::seed_resource_group(pool, &rg, &tenant).await;
    // The evidence routes admit the platform service key only (a
    // tenant's shared key used to pass as it).
    let service_key = common::seed_key(pool, &tenant, None, true, "system-health-check").await;
    let state = common::build_test_state(pool.clone());
    let app = Router::new()
        // Same composition as `main.rs`: the AAC export rides in the nest.
        .nest(
            "/v1/evidence",
            routes::evidence::router().merge(routes::evidence_aac::router()),
        )
        .nest("/.well-known", routes::evidence::well_known_router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .with_state(state.clone());
    Env {
        app,
        service_key,
        tenant,
        state,
    }
}

async fn call(env: &Env, method: &str, path: &str, body: Option<Value>) -> (StatusCode, Value) {
    call_as(env, method, path, body, Some(&env.service_key)).await
}

async fn call_as(
    env: &Env,
    method: &str,
    path: &str,
    body: Option<Value>,
    bearer: Option<&str>,
) -> (StatusCode, Value) {
    let mut b = Request::builder()
        .method(method)
        .uri(path)
        .header("content-type", "application/json");
    if let Some(k) = bearer {
        b = b.header("authorization", format!("Bearer {k}"));
    }
    let mut req = b
        .body(match body {
            Some(v) => Body::from(serde_json::to_vec(&v).unwrap()),
            None => Body::empty(),
        })
        .unwrap();
    let peer: std::net::SocketAddr = "127.0.0.1:55555".parse().unwrap();
    req.extensions_mut()
        .insert(axum::extract::ConnectInfo(peer));
    let resp = env.app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 64 * 1024 * 1024)
        .await
        .unwrap();
    let v = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, v)
}

async fn seal(env: &Env, records: Value) -> (StatusCode, Value) {
    call(
        env,
        "POST",
        "/v1/evidence/seal",
        Some(json!({"tenant_id": env.tenant, "records": records})),
    )
    .await
}

fn vk_from_hex(kid: &str) -> VerifyingKey {
    VerifyingKey::from_bytes(&hex::decode(kid).unwrap().try_into().unwrap()).unwrap()
}

// ---------------------------------------------------------------------------
// Seal API
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn seal_api_seals_validates_and_reports_disabled(pool: PgPool) {
    let env = setup(&pool).await;

    let (st, body) = seal(
        &env,
        json!([
            {
                "record_type": "ai.brutor.declaration",
                "context": {"framework_id": "eu-ai-act", "obligation_key": "art13"},
                "payload": {"rationale": "SECRET-RATIONALE-TEXT", "score": 0.75},
            },
            {
                "record_type": "ai.brutor.incident",
                "context": {"incident_id": "inc-1", "state": "open"},
                "disposition": {"decision":"accept","approver":"human","human_disposed":true,"authority":"adm-1"},
            }
        ]),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{body}");
    let sealed = body["sealed"].as_array().unwrap();
    assert_eq!(sealed.len(), 2);
    let vk = deployment_key().verifying_key();
    for s in sealed {
        let rec = &s["record"];
        assert!(!rec.to_string().contains("SECRET-RATIONALE-TEXT"));
        let statement = unb64(s["statement_b64"].as_str().unwrap());
        let info = verify_record_statement(&statement, rec, &vk).expect("statement verifies");
        assert_eq!(info.record_id, s["record_id"].as_str().unwrap());
        assert_eq!(s["log_id"], key::log_id_for(&env.tenant));
    }
    assert_eq!(sealed[0]["seq"], 0);
    assert_eq!(sealed[1]["seq"], 1);
    let digest = sealed[0]["payload_digest"].as_str().unwrap();
    assert_eq!(sealed[0]["record"]["references"][0]["digest"], digest);
    assert!(sealed[1].get("payload_digest").is_none());

    let rows: Vec<(String, String, String, Option<String>)> = sqlx::query_as(
        "SELECT record_id, verdict, surface, proxy_log_id FROM action_records \
         WHERE tenant_id = $1 ORDER BY seq",
    )
    .bind(&env.tenant)
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].1, "recorded");
    assert_eq!(rows[0].2, "control_plane");
    assert!(rows[0].3.is_none());
    // Cadence 2 ⇒ the two records were covered by a head in the same call.
    let (heads,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM evidence_tree_heads WHERE tenant_id = $1")
            .bind(&env.tenant)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(heads, 1);

    // 422s — the allow-list, unknown members, the honesty invariant, an
    // unknown AI System. Nothing lands.
    for (bad, needle) in [
        (
            json!([{"record_type":"ai.brutor.declaration","context":{"subject_id":"u-7"}}]),
            "subject_id",
        ),
        (
            json!([{"record_type":"ai.brutor.declaration","user_email":"a@b.c"}]),
            "user_email",
        ),
        (
            json!([{"record_type":"ai.brutor.run-abort","verdict":"resolved",
                    "disposition":{"decision":"reject","approver":"policy","human_disposed":true}}]),
            "human_disposed",
        ),
        (
            json!([{"record_type":"ai.brutor.declaration","ai_system_id":"rg-nope"}]),
            "ai_system_id",
        ),
    ] {
        let (st, body) = seal(&env, bad).await;
        assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY, "{body}");
        assert_eq!(body["error"], "invalid_record");
        assert!(
            body["detail"].as_str().unwrap().contains(needle),
            "{needle}: {body}"
        );
    }
    let too_many: Vec<Value> = (0..101)
        .map(|_| json!({"record_type": "ai.brutor.report"}))
        .collect();
    let (st, _) = seal(&env, json!(too_many)).await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
    let (n,): (i64,) = sqlx::query_as("SELECT count(*) FROM action_records WHERE tenant_id = $1")
        .bind(&env.tenant)
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(n, 2, "rejected requests seal nothing");

    // 503 — no key (task-local, so concurrent tests keep theirs).
    let (st, body) = key::FORCE_DISABLED
        .scope(
            true,
            seal(&env, json!([{"record_type": "ai.brutor.report"}])),
        )
        .await;
    assert_eq!(st, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"], "evidence_disabled");

    // No credential ⇒ 401.
    let (st, _) = call_as(
        &env,
        "POST",
        "/v1/evidence/seal",
        Some(json!({"tenant_id": env.tenant, "records": []})),
        None,
    )
    .await;
    assert_eq!(st, StatusCode::UNAUTHORIZED);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn seal_api_attaches_the_ai_system_contract(pool: PgPool) {
    let env = setup(&pool).await;
    let sys = format!("rg-sys-{}", Ulid::new()).to_lowercase();
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, name, \
         is_active, is_system, group_type, system_kind) \
         VALUES ($1, $2, 0, true, $1, true, false, CAST('AI_SYSTEM' AS public.grouptype), 'assistant')",
    )
    .bind(&sys)
    .bind(&env.tenant)
    .execute(&pool)
    .await
    .unwrap();
    let hash = "d".repeat(64);
    sqlx::query(
        "INSERT INTO ai_system_contracts \
         (id, tenant_id, ai_system_id, version, contract_hash, resolved_closure, status) \
         VALUES ($1, $2, $3, 4, $4, '{}'::jsonb, 'active')",
    )
    .bind(format!("ctr-{}", Ulid::new()))
    .bind(&env.tenant)
    .bind(&sys)
    .bind(&hash)
    .execute(&pool)
    .await
    .unwrap();
    let (st, body) = seal(
        &env,
        json!([{"record_type":"ai.brutor.epoch-boundary","verdict":"epoch_boundary",
                "ai_system_id": sys, "context": {"contract_version": 4}}]),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{body}");
    let rec = &body["sealed"][0]["record"];
    assert_eq!(rec["system"]["ai_system_id"], sys.as_str());
    let (ai_system_id,): (Option<String>,) =
        sqlx::query_as("SELECT ai_system_id FROM action_records WHERE record_id = $1")
            .bind(body["sealed"][0]["record_id"].as_str().unwrap())
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(ai_system_id.as_deref(), Some(sys.as_str()));
    // The active contract binds the record (`ai_system_contracts.version`
    // is INTEGER — a decode mismatch once dropped these silently).
    assert_eq!(rec["system"]["contract_hash"], hash.as_str(), "{rec}");
    assert_eq!(rec["system"]["contract_version"], 4);
    assert_eq!(rec["epoch"], format!("contract:{hash}"));
}

// ---------------------------------------------------------------------------
// Forced tree head + verify family
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn forced_head_covers_the_log_and_verifies(pool: PgPool) {
    let env = setup(&pool).await;
    let path = format!("/v1/evidence/logs/{}/tree-head", env.tenant);
    // An empty (absent) log: nothing to cover.
    let (st, body) = call(&env, "POST", &path, None).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(body["issued"], false);

    let (st, sealed) = seal(&env, json!([{"record_type": "ai.brutor.report"}])).await;
    assert_eq!(st, StatusCode::OK);
    // Cadence 2, one record ⇒ uncovered until forced.
    let (st, body) = call(&env, "POST", &path, None).await;
    assert_eq!(st, StatusCode::OK, "{body}");
    assert_eq!(body["issued"], true);
    assert_eq!(body["head"]["tree_size"], 1);
    let head_b64 = body["head"]["statement_b64"].as_str().unwrap().to_string();
    let (last, size): (Option<String>, i64) = sqlx::query_as(
        "SELECT last_tree_head_id, tree_size FROM evidence_logs WHERE tenant_id = $1",
    )
    .bind(&env.tenant)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(last.as_deref(), body["head"]["id"].as_str());
    assert_eq!(size, 2, "the record plus the head's stamp");
    // Covered ⇒ forcing again issues nothing.
    let (_, again) = call(&env, "POST", &path, None).await;
    assert_eq!(again["issued"], false);

    // verify/tree-head
    let (st, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/tree-head",
        Some(json!({"statement_b64": head_b64})),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(v["verified"], true, "{v}");
    assert_eq!(v["log_id"], key::log_id_for(&env.tenant));
    assert_eq!(v["tree_size"], 1);

    // A second head chains to the first.
    seal(&env, json!([{"record_type": "ai.brutor.report"}])).await;
    let (_, second) = call(&env, "POST", &path, None).await;
    assert_eq!(second["issued"], true);
    let (_, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/tree-head",
        Some(json!({
            "statement_b64": second["head"]["statement_b64"],
            "previous_statement_b64": head_b64,
        })),
    )
    .await;
    assert_eq!(v["verified"], true, "{v}");
    assert_eq!(v["consistent_with_previous"], true);
    // Pinning a stranger's key makes the same head unverified.
    let (_, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/tree-head",
        Some(json!({"statement_b64": head_b64, "trusted_kids": ["00".repeat(32)]})),
    )
    .await;
    assert_eq!(v["verified"], false);

    // verify/statement on a record statement, with and without the record.
    let s0 = &sealed["sealed"][0];
    let (_, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/statement",
        Some(json!({"statement_b64": s0["statement_b64"], "record": s0["record"]})),
    )
    .await;
    assert_eq!(v["verified"], true, "{v}");
    assert_eq!(v["record_id"], s0["record_id"]);
    assert_eq!(v["sub"], format!("tenant:{}", env.tenant));
    let mut tampered = s0["record"].clone();
    tampered["verdict"] = json!("denied");
    let (_, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/statement",
        Some(json!({"statement_b64": s0["statement_b64"], "record": tampered})),
    )
    .await;
    assert_eq!(v["verified"], false);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn verify_receipt_grades_rfc3161_tokens_against_a_witness_pin(pool: PgPool) {
    let env = setup(&pool).await;
    let dir = crate::common::community_root()
        .join("tests/fixtures/rfc3161")
        .display()
        .to_string();
    let token = std::fs::read(format!("{dir}/rsa2048.tsr")).unwrap();
    let statement = std::fs::read(format!("{dir}/statement.bin")).unwrap();
    let fp = std::fs::read_to_string(format!("{dir}/rsa2048.crt.sha256")).unwrap();
    let wid = format!("wit-{}", Ulid::new());
    sqlx::query(
        "INSERT INTO witnesses (id, scope, kind, url, operator_label, pinned_key, enabled) \
         VALUES ($1, 'platform', 'rfc3161', 'https://tsa.invalid/', 'Test TSA', $2, false)",
    )
    .bind(&wid)
    .bind(fp.trim())
    .execute(&pool)
    .await
    .unwrap();
    let (st, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/receipt",
        Some(json!({"receipt_b64": b64(&token), "statement_b64": b64(&statement), "witness_id": wid})),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(v["grade"], "timestamped", "{v}");
    assert_eq!(v["kind"], "rfc3161");
    assert!(v["iat"].as_i64().unwrap() > 1_700_000_000);
    // Another statement ⇒ invalid; no pin ⇒ unverified.
    let (_, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/receipt",
        Some(
            json!({"receipt_b64": b64(&token), "statement_b64": b64(b"other"), "witness_id": wid}),
        ),
    )
    .await;
    assert_eq!(v["grade"], "invalid");
    let (_, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/receipt",
        Some(json!({"receipt_b64": b64(&token), "statement_b64": b64(&statement)})),
    )
    .await;
    assert_eq!(v["grade"], "unverified");
}

/// `witnesses.pinned_key` is TEXT so an rfc3161 pin can be the
/// TSA certificate PEM itself, not only its fingerprint.
#[sqlx::test(migrations = "./tests/migrations")]
async fn verify_receipt_accepts_a_pem_certificate_pin_from_the_witness_row(pool: PgPool) {
    let env = setup(&pool).await;
    let dir = crate::common::community_root()
        .join("tests/fixtures/rfc3161")
        .display()
        .to_string();
    let token = std::fs::read(format!("{dir}/rsa2048.tsr")).unwrap();
    let statement = std::fs::read(format!("{dir}/statement.bin")).unwrap();
    let pem = std::fs::read_to_string(format!("{dir}/rsa2048.crt")).unwrap();
    assert!(
        pem.len() > 128,
        "the pin under test does not fit the old column"
    );
    let wid = format!("wit-{}", Ulid::new());
    sqlx::query(
        "INSERT INTO witnesses (id, scope, kind, url, operator_label, pinned_key, enabled) \
         VALUES ($1, 'platform', 'rfc3161', 'https://tsa.invalid/', 'Test TSA', $2, false)",
    )
    .bind(&wid)
    .bind(&pem)
    .execute(&pool)
    .await
    .expect("a PEM pin fits witnesses.pinned_key (TEXT)");
    let (st, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/receipt",
        Some(json!({"receipt_b64": b64(&token), "statement_b64": b64(&statement), "witness_id": wid})),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(v["grade"], "timestamped", "{v}");
    // The ECDSA certificate is a different TSA: its PEM pin rejects the token.
    let other = std::fs::read_to_string(format!("{dir}/ecp256.crt")).unwrap();
    sqlx::query("UPDATE witnesses SET pinned_key = $2 WHERE id = $1")
        .bind(&wid)
        .bind(&other)
        .execute(&pool)
        .await
        .unwrap();
    let (_, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/receipt",
        Some(json!({"receipt_b64": b64(&token), "statement_b64": b64(&statement), "witness_id": wid})),
    )
    .await;
    assert_eq!(v["grade"], "invalid", "{v}");
}

// ---------------------------------------------------------------------------
// Bundles + disclosure
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn bundle_end_to_end_with_closure_and_disclosure(pool: PgPool) {
    let env = setup(&pool).await;
    // P lives in another run; C (in run-x) supersedes it.
    let (_, p) = seal(
        &env,
        json!([{"record_type":"ai.brutor.assessment","verdict":"assessment",
                "context":{"root_task_id":"run-other","check_id":"chk-1"}}]),
    )
    .await;
    let parent = p["sealed"][0]["record_id"].as_str().unwrap().to_string();
    let (st, c) = seal(
        &env,
        json!([
            {"record_type":"ai.brutor.adjudication","verdict":"adjudication",
             "context":{"root_task_id":"run-x","check_id":"chk-1"},
             "disposition":{"decision":"accept","approver":"human","human_disposed":true},
             "chain":{"parent_record_id": parent, "relation":"adjudicates"}},
            {"record_type":"ai.brutor.report","context":{"root_task_id":"run-x"}},
            {"record_type":"ai.brutor.report","context":{"root_task_id":"run-x"}}
        ]),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{c}");

    // payloads: all ⇒ 409, bodies never enter records.
    let (st, body) = call(
        &env,
        "POST",
        "/v1/evidence/bundles",
        Some(json!({"tenant_id": env.tenant, "selector": {"root_task_id": "run-x"}, "payloads": "all"})),
    )
    .await;
    assert_eq!(st, StatusCode::CONFLICT);
    assert_eq!(body["error"], "payloads_unavailable");

    let (st, built) = call(
        &env,
        "POST",
        "/v1/evidence/bundles",
        Some(json!({
            "tenant_id": env.tenant,
            "selector": {"root_task_id": "run-x"},
            "audience": "Auditor X",
            "framework_id": "eu-ai-act",
            "catalog_version": "2026-09",
            "closure_depth": 1,
            "requested_by": "adm-1",
            "extensions": {"ai.brutor/evidence-report/v1": {"rows": 3}},
        })),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{built}");
    let bundle = built["bundle"].clone();
    let digest = built["bundle_digest"].as_str().unwrap().to_string();
    assert_eq!(
        bundle["records"].as_array().unwrap().len(),
        4,
        "3 + the closed-over parent"
    );
    assert_eq!(
        bundle["manifest"]["completeness"]["selection"],
        "contiguous"
    );
    assert_eq!(
        bundle["manifest"]["completeness"]["records_mode"],
        "complete"
    );
    assert!(!bundle["tree_heads"].as_array().unwrap().is_empty());
    assert_eq!(built["persisted"], true);

    // Verify through the API: ok; tampered: not ok; wrong digest: not ok.
    let (st, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/bundle",
        Some(json!({"bundle": bundle, "bundle_digest": digest})),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(v["ok"], true, "{v}");
    assert_eq!(v["records_verified"], 4);
    assert_eq!(v["proofs_verified"], 4);
    let mut tampered = bundle.clone();
    tampered["records"][1]["record"]["verdict"] = json!("executed");
    let (_, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/bundle",
        Some(json!({"bundle": tampered})),
    )
    .await;
    assert_eq!(v["ok"], false);
    let (_, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/bundle",
        Some(json!({"bundle": bundle, "bundle_digest": "0".repeat(64)})),
    )
    .await;
    assert_eq!(v["ok"], false);

    // The digest is sealed as an ai.brutor.bundle record …
    let (brec,): (Value,) =
        sqlx::query_as("SELECT record FROM action_records WHERE record_id = $1")
            .bind(built["record_id"].as_str().unwrap())
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(brec["context"]["record_type"], "ai.brutor.bundle");
    assert_eq!(brec["references"][0]["digest"], digest.as_str());
    // … the disclosure is recorded, sealed into the log as a `disclosure` entry …
    let did = built["disclosure_id"].as_str().unwrap();
    let (aud, seq, completeness, ids, ddigest): (
        String,
        Option<i64>,
        String,
        Value,
        Option<String>,
    ) = sqlx::query_as(
        "SELECT audience, seq, completeness, disclosed_record_ids, bundle_digest \
             FROM disclosure_records WHERE id = $1",
    )
    .bind(did)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(aud, "Auditor X");
    assert_eq!(completeness, "contiguous");
    assert_eq!(ids.as_array().unwrap().len(), 4);
    assert_eq!(ddigest.as_deref(), Some(digest.as_str()));
    let (kind, ref_id): (String, Option<String>) = sqlx::query_as(
        "SELECT kind, ref_id FROM evidence_log_entries WHERE log_id = $1 AND seq = $2",
    )
    .bind(key::log_id_for(&env.tenant))
    .bind(seq.unwrap())
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(kind, "disclosure");
    assert_eq!(ref_id.as_deref(), Some(did));
    // … and the bundle is persisted, with the audience label never in a record.
    let (count, bdigest, sel): (i32, String, String) = sqlx::query_as(
        "SELECT record_count, bundle_digest, selection FROM evidence_bundles WHERE id = $1",
    )
    .bind(built["bundle_id"].as_str().unwrap())
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        (count, bdigest.as_str(), sel.as_str()),
        (4, digest.as_str(), "contiguous")
    );
    let (leaks,): (i64,) = sqlx::query_as(
        "SELECT count(*) FROM action_records WHERE tenant_id = $1 AND record::text LIKE '%Auditor X%'",
    )
    .bind(&env.tenant)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(leaks, 0);

    // closure_depth 0: the parent is declared missing, never dropped, and
    // the bundle still verifies as declared-incomplete.
    let (st, shallow) = call(
        &env,
        "POST",
        "/v1/evidence/bundles",
        Some(json!({"tenant_id": env.tenant, "selector": {"root_task_id": "run-x"}, "closure_depth": 0})),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{shallow}");
    let m = &shallow["bundle"]["manifest"]["completeness"];
    assert_eq!(m["records_mode"], "declared_incomplete");
    assert_eq!(m["missing"], json!([parent]));
    assert!(
        shallow.get("disclosure_id").is_none(),
        "no audience ⇒ no disclosure"
    );
    let (_, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/bundle",
        Some(json!({"bundle": shallow["bundle"], "bundle_digest": shallow["bundle_digest"]})),
    )
    .await;
    assert_eq!(v["ok"], true, "{v}");
    assert_eq!(v["missing"], json!([parent]));

    // Explicit ids are producer-selected; an unknown id is listed missing.
    let ghost = "e".repeat(64);
    let (st, picked) = call(
        &env,
        "POST",
        "/v1/evidence/bundles",
        Some(json!({"tenant_id": env.tenant, "selector": {"record_ids": [parent, ghost]}})),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{picked}");
    let m = &picked["bundle"]["manifest"]["completeness"];
    assert_eq!(m["selection"], "producer-selected");
    assert_eq!(m["missing"], json!([ghost]));

    // Nothing matches ⇒ 404.
    let (st, _) = call(
        &env,
        "POST",
        "/v1/evidence/bundles",
        Some(json!({"tenant_id": env.tenant, "selector": {"root_task_id": "run-none"}})),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_FOUND);
}

// ---------------------------------------------------------------------------
// Backfill
// ---------------------------------------------------------------------------

fn mcp_row(tenant: &str, method: &str) -> ProxyLogInsert {
    let auth = test_auth(tenant);
    let mut log = proxy_logger::build_proxy_log(
        "mcp",
        &auth,
        "POST",
        "/v1/proxy/mcp/srv-1",
        None,
        Some("srv-1"),
        200,
        9.0,
        true,
        None,
        &format!("corr-{}", Ulid::new()),
    );
    log.mcp_method = Some(method.to_string());
    log.mcp_capability_name = Some("write_order".into());
    log.root_task_id = Some(format!("run-{}", Ulid::new()));
    log
}

async fn write_rows(pool: &PgPool, rows: Vec<ProxyLogInsert>) {
    let ids: Vec<String> = rows.iter().map(|r| r.id.clone()).collect();
    let writer = BackgroundWriter::new();
    for r in rows {
        writer.write_proxy_log(pool.clone(), r);
    }
    for _ in 0..100 {
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

#[sqlx::test(migrations = "./tests/migrations")]
async fn backfill_seals_unsealed_rows_once(pool: PgPool) {
    let env = setup(&pool).await;
    let lost = mcp_row(&env.tenant, "tools/call"); // record deleted: must be re-sealed
    let unlinked = mcp_row(&env.tenant, "tools/call"); // record exists, link lost
    let query = mcp_row(&env.tenant, "tools/list"); // never sealed
    let (lost_id, unlinked_id, query_id) = (lost.id.clone(), unlinked.id.clone(), query.id.clone());
    write_rows(&pool, vec![lost, unlinked, query]).await;
    let rid = |id: String| {
        let pool = pool.clone();
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
    let unlinked_rec = rid(unlinked_id.clone())
        .await
        .expect("sealed by the writer");
    // Simulate the two gaps backfill exists for.
    sqlx::query("DELETE FROM action_records WHERE proxy_log_id = $1")
        .bind(&lost_id)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("UPDATE proxy_logs SET record_id = NULL WHERE id = ANY($1)")
        .bind(vec![lost_id.clone(), unlinked_id.clone()])
        .execute(&pool)
        .await
        .unwrap();

    let window = json!({
        "tenant_id": env.tenant,
        "from": (crate::common::db_now() - chrono::Duration::hours(1)).to_rfc3339(),
        "to": (crate::common::db_now() + chrono::Duration::minutes(5)).to_rfc3339(),
        "limit": 100,
    });
    let (st, report) = call(&env, "POST", "/v1/evidence/backfill", Some(window.clone())).await;
    assert_eq!(st, StatusCode::OK, "{report}");
    assert_eq!(report["sealed"], 1, "{report}");
    assert_eq!(report["relinked"], 1);
    assert_eq!(report["not_governed"], 1);
    assert_eq!(report["failed"], 0);
    assert_eq!(
        rid(unlinked_id.clone()).await.as_deref(),
        Some(unlinked_rec.as_str())
    );
    let new_rec = rid(lost_id.clone()).await.expect("backfilled");
    assert!(rid(query_id.clone()).await.is_none());
    let (record,): (Value,) =
        sqlx::query_as("SELECT record FROM action_records WHERE record_id = $1")
            .bind(&new_rec)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(record["context"]["backfilled"], true);
    assert!(brutor_evidence::verify(&record, None).ok);

    // Idempotent: a second run seals nothing new.
    let (st, again) = call(&env, "POST", "/v1/evidence/backfill", Some(window)).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(again["sealed"], 0, "{again}");
    assert_eq!(again["relinked"], 0);
    let (n,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM action_records WHERE proxy_log_id = $1")
            .bind(&lost_id)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(n, 1);

    // Bounds are enforced.
    let (st, _) = call(
        &env,
        "POST",
        "/v1/evidence/backfill",
        Some(json!({"tenant_id": env.tenant, "from": "2026-09-19T00:00:00Z", "to": "2026-09-20T00:00:00Z", "limit": 10001})),
    )
    .await;
    assert_eq!(st, StatusCode::UNPROCESSABLE_ENTITY);
}

/// A window with more never-sealed rows than `limit` must still be
/// finishable: those rows keep `record_id IS NULL`, so a scan that always
/// restarts at `from` would rescan them forever and never reach the
/// governed row behind them. The truncated report hands back a cursor.
#[sqlx::test(migrations = "./tests/migrations")]
async fn backfill_resumes_past_never_sealed_rows_with_a_cursor(pool: PgPool) {
    let env = setup(&pool).await;
    // Three queries (never sealed) written before one command whose record
    // is then lost.
    let q1 = mcp_row(&env.tenant, "tools/list");
    let q2 = mcp_row(&env.tenant, "tools/list");
    let q3 = mcp_row(&env.tenant, "tools/list");
    write_rows(&pool, vec![q1, q2, q3]).await;
    tokio::time::sleep(Duration::from_millis(20)).await;
    let lost = mcp_row(&env.tenant, "tools/call");
    let lost_id = lost.id.clone();
    write_rows(&pool, vec![lost]).await;
    sqlx::query("DELETE FROM action_records WHERE proxy_log_id = $1")
        .bind(&lost_id)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("UPDATE proxy_logs SET record_id = NULL WHERE id = $1")
        .bind(&lost_id)
        .execute(&pool)
        .await
        .unwrap();

    let mut window = json!({
        "tenant_id": env.tenant,
        "from": (crate::common::db_now() - chrono::Duration::hours(1)).to_rfc3339(),
        "to": (crate::common::db_now() + chrono::Duration::minutes(5)).to_rfc3339(),
        "limit": 2,
    });
    // First page: two queries, nothing sealed, truncated with a cursor.
    let (st, first) = call(&env, "POST", "/v1/evidence/backfill", Some(window.clone())).await;
    assert_eq!(st, StatusCode::OK, "{first}");
    assert_eq!(first["scanned"], 2);
    assert_eq!(first["sealed"], 0);
    assert_eq!(first["truncated"], true);
    assert!(first["next_cursor"]["id"].is_string(), "{first}");
    // Without the cursor the same page comes back — the bug this guards.
    let (_, again) = call(&env, "POST", "/v1/evidence/backfill", Some(window.clone())).await;
    assert_eq!(again["next_cursor"], first["next_cursor"]);
    // With it, the scan continues and reaches the governed row.
    window["after"] = first["next_cursor"].clone();
    let (st, second) = call(&env, "POST", "/v1/evidence/backfill", Some(window.clone())).await;
    assert_eq!(st, StatusCode::OK, "{second}");
    assert_eq!(second["scanned"], 2, "{second}");
    assert_eq!(second["sealed"], 1, "{second}");
    assert_eq!(second["not_governed"], 1);
    assert_eq!(second["truncated"], false);
    assert!(second.get("next_cursor").is_none(), "{second}");
    let (rid,): (Option<String>,) =
        sqlx::query_as("SELECT record_id FROM proxy_logs WHERE id = $1")
            .bind(&lost_id)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert!(
        rid.is_some(),
        "the row behind the never-sealed rows was reached"
    );
}

// ---------------------------------------------------------------------------
// Per-tenant keys
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn per_tenant_key_rotation_keeps_old_records_verifiable(pool: PgPool) {
    let env = setup(&pool).await;
    let rotate = format!("/v1/evidence/keys/{}/rotate", env.tenant);
    // Not opted in ⇒ 409.
    let (st, body) = call(&env, "POST", &rotate, None).await;
    assert_eq!(st, StatusCode::CONFLICT, "{body}");
    sqlx::query("INSERT INTO evidence_settings (tenant_id, per_tenant_key) VALUES ($1, true)")
        .bind(&env.tenant)
        .execute(&pool)
        .await
        .unwrap();

    let deployment_kid = hex::encode(deployment_key().verifying_key().to_bytes());
    let (_, a) = seal(&env, json!([{"record_type": "ai.brutor.report"}])).await;
    let a = a["sealed"][0].clone();

    // First rotation creates the tenant key; the hand-over is signed by the
    // deployment key.
    let (st, r1) = call(&env, "POST", &rotate, None).await;
    assert_eq!(st, StatusCode::OK, "{r1}");
    assert_eq!(r1["previous_kid"], deployment_kid.as_str());
    let kid1 = r1["kid"].as_str().unwrap().to_string();
    assert_ne!(kid1, deployment_kid);
    let (rot_rec, rot_stmt, rot_kid): (Value, Vec<u8>, String) =
        sqlx::query_as("SELECT record, statement, kid FROM action_records WHERE record_id = $1")
            .bind(r1["rotation_record_id"].as_str().unwrap())
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(rot_kid, deployment_kid);
    assert_eq!(rot_rec["context"]["record_type"], "ai.brutor.key-rotation");
    assert_eq!(rot_rec["context"]["new_kid"], kid1.as_str());
    verify_record_statement(&rot_stmt, &rot_rec, &deployment_key().verifying_key())
        .expect("rotation record is signed by the old key");

    // New records use the tenant key.
    let (_, b) = seal(&env, json!([{"record_type": "ai.brutor.report"}])).await;
    let b = b["sealed"][0].clone();
    let (b_kid,): (String,) = sqlx::query_as("SELECT kid FROM action_records WHERE record_id = $1")
        .bind(b["record_id"].as_str().unwrap())
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(b_kid, kid1);
    verify_record_statement(
        &unb64(b["statement_b64"].as_str().unwrap()),
        &b["record"],
        &vk_from_hex(&kid1),
    )
    .expect("new record verifies under the tenant key");
    let (seed_ct,): (String,) =
        sqlx::query_as("SELECT encrypted_seed FROM evidence_signing_keys WHERE kid = $1")
            .bind(&kid1)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert!(!seed_ct.contains(&kid1), "the seed is stored encrypted");

    // Rotate again: kid1 retires, kid2 is active.
    let (st, r2) = call(&env, "POST", &rotate, None).await;
    assert_eq!(st, StatusCode::OK, "{r2}");
    assert_eq!(r2["previous_kid"], kid1.as_str());
    let kid2 = r2["kid"].as_str().unwrap().to_string();
    let (rot2_kid,): (String,) =
        sqlx::query_as("SELECT kid FROM action_records WHERE record_id = $1")
            .bind(r2["rotation_record_id"].as_str().unwrap())
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        rot2_kid, kid1,
        "the second hand-over is signed by the retiring tenant key"
    );
    let statuses: Vec<(String, String)> = sqlx::query_as(
        "SELECT kid, status FROM evidence_signing_keys WHERE tenant_id = $1 ORDER BY created_at",
    )
    .bind(&env.tenant)
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(
        statuses,
        vec![
            (kid1.clone(), "retired".into()),
            (kid2.clone(), "active".into())
        ]
    );

    // Old records (deployment key, retired tenant key) still verify.
    for rec in [&a, &b] {
        let (_, v) = call(
            &env,
            "POST",
            "/v1/evidence/verify/record",
            Some(json!({"record": rec["record"], "statement_b64": rec["statement_b64"]})),
        )
        .await;
        assert_eq!(v["ok"], true, "{v}");
    }
    let (_, c) = seal(&env, json!([{"record_type": "ai.brutor.report"}])).await;
    let (_, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/record",
        Some(json!({"record": c["sealed"][0]["record"], "statement_b64": c["sealed"][0]["statement_b64"]})),
    )
    .await;
    assert_eq!(v["statement"]["kid"], kid2.as_str());
    assert_eq!(v["statement"]["kid_status"], "active");

    // The public document lists every kid by log_id, never by tenant id.
    let (st, doc) = call_as(
        &env,
        "GET",
        "/.well-known/brutor-evidence-keys.json",
        None,
        None,
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    let keys = doc["keys"].as_array().unwrap();
    let find = |k: &str| keys.iter().find(|x| x["kid"] == k).cloned();
    assert_eq!(find(&kid1).unwrap()["status"], "retired");
    assert_eq!(find(&kid2).unwrap()["status"], "active");
    assert_eq!(find(&kid2).unwrap()["log_id"], key::log_id_for(&env.tenant));
    assert_eq!(find(&deployment_kid).unwrap()["status"], "active");
    assert!(!doc.to_string().contains(&env.tenant));
}

// ---------------------------------------------------------------------------
// Row-chain heads
// ---------------------------------------------------------------------------

/// A tenant that signs with its own key is never
/// sealed under another one. A stored seed that did not decrypt (another
/// `ENCRYPTION_KEY`, a damaged row) used to "fall back to the deployment
/// key" with a warning: the tenant's records and tree heads went on being
/// sealed, under a key the tenant's verifier does not pin. Nothing is sealed
/// until the key is usable again; the rows land unsealed and `backfill`
/// seals them later, under the right key. Rotation is the way out.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unusable_tenant_key_seals_nothing_instead_of_using_the_deployment_key(pool: PgPool) {
    let env = setup(&pool).await;
    let rotate = format!("/v1/evidence/keys/{}/rotate", env.tenant);
    sqlx::query("INSERT INTO evidence_settings (tenant_id, per_tenant_key) VALUES ($1, true)")
        .bind(&env.tenant)
        .execute(&pool)
        .await
        .unwrap();
    let (st, r1) = call(&env, "POST", &rotate, None).await;
    assert_eq!(st, StatusCode::OK, "{r1}");
    let kid1 = r1["kid"].as_str().unwrap().to_string();
    let deployment_kid = hex::encode(deployment_key().verifying_key().to_bytes());

    // The seed no longer decrypts.
    sqlx::query("UPDATE evidence_signing_keys SET encrypted_seed = 'not-a-seed' WHERE kid = $1")
        .bind(&kid1)
        .execute(&pool)
        .await
        .unwrap();
    key::invalidate_tenant(&pool, &env.tenant).await;
    let sealed_by = |kid: String| {
        let pool = pool.clone();
        let tenant = env.tenant.clone();
        async move {
            let (n,): (i64,) = sqlx::query_as(
                "SELECT count(*) FROM action_records WHERE tenant_id = $1 AND kid = $2",
            )
            .bind(&tenant)
            .bind(&kid)
            .fetch_one(&pool)
            .await
            .unwrap();
            n
        }
    };
    // The hand-over record of the first rotation, and nothing else.
    let by_deployment = sealed_by(deployment_kid.clone()).await;

    // The seal API refuses and says why.
    let (st, body) = seal(&env, json!([{"record_type": "ai.brutor.report"}])).await;
    assert_eq!(st, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(body["error"], "tenant_key_unavailable", "{body}");
    assert!(
        !body.to_string().contains("not-a-seed"),
        "the refusal must not carry the stored seed: {body}"
    );
    // So does a forced tree head.
    let (st, body) = call(
        &env,
        "POST",
        &format!("/v1/evidence/logs/{}/tree-head", env.tenant),
        None,
    )
    .await;
    assert_eq!(st, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    // The writer's row lands, unsealed.
    let row = mcp_row(&env.tenant, "tools/call");
    let row_id = row.id.clone();
    write_rows(&pool, vec![row]).await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    let (rid,): (Option<String>,) =
        sqlx::query_as("SELECT record_id FROM proxy_logs WHERE id = $1")
            .bind(&row_id)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        rid, None,
        "the row was sealed although the tenant key is unusable"
    );
    assert_eq!(
        sealed_by(deployment_kid.clone()).await,
        by_deployment,
        "a record of this tenant was sealed under the deployment key"
    );

    // Rotation is the way out: the hand-over is signed by the deployment
    // key (the unusable key cannot sign it) and says so.
    let (st, r2) = call(&env, "POST", &rotate, None).await;
    assert_eq!(st, StatusCode::OK, "{r2}");
    assert_eq!(r2["previous_kid"], deployment_kid.as_str());
    let kid2 = r2["kid"].as_str().unwrap().to_string();
    let (st, body) = seal(&env, json!([{"record_type": "ai.brutor.report"}])).await;
    assert_eq!(st, StatusCode::OK, "{body}");
    // And the unsealed row is backfilled under the tenant's new key.
    let window = json!({
        "tenant_id": env.tenant,
        "from": (crate::common::db_now() - chrono::Duration::hours(1)).to_rfc3339(),
        "to": (crate::common::db_now() + chrono::Duration::minutes(5)).to_rfc3339(),
        "limit": 100,
    });
    let (st, report) = call(&env, "POST", "/v1/evidence/backfill", Some(window)).await;
    assert_eq!(st, StatusCode::OK, "{report}");
    let (row_kid,): (String,) = sqlx::query_as(
        "SELECT r.kid FROM proxy_logs p JOIN action_records r ON r.record_id = p.record_id \
         WHERE p.id = $1",
    )
    .bind(&row_id)
    .fetch_one(&pool)
    .await
    .expect("the row is sealed by the backfill");
    assert_eq!(row_kid, kid2);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn tree_heads_seal_the_row_chain_head(pool: PgPool) {
    let env = setup(&pool).await;
    write_rows(&pool, vec![mcp_row(&env.tenant, "tools/call")]).await;
    let (chain_id, seq): (String, i64) = sqlx::query_as(
        "SELECT chain_id, chain_seq FROM proxy_logs WHERE tenant_id = $1 AND chain_id IS NOT NULL \
         ORDER BY chain_seq DESC LIMIT 1",
    )
    .bind(&env.tenant)
    .fetch_one(&pool)
    .await
    .unwrap();
    let head_hash = "a".repeat(64);
    sqlx::query(
        "INSERT INTO audit_chain_checkpoints (id, chain_id, seq_start, seq_end, head_hash, signature, public_key) \
         VALUES ($1, $2, 0, $3, $4, $5, $6)",
    )
    .bind(format!("ckpt-{}", Ulid::new()))
    .bind(&chain_id)
    .bind(seq + 5)
    .bind(&head_hash)
    .bind("b".repeat(128))
    .bind("c".repeat(64))
    .execute(&pool)
    .await
    .unwrap();

    let path = format!("/v1/evidence/logs/{}/tree-head", env.tenant);
    let (_, body) = call(&env, "POST", &path, None).await;
    assert_eq!(body["issued"], true, "{body}");
    let rows: Vec<(Value, i64)> = sqlx::query_as(
        "SELECT record, seq FROM action_records WHERE tenant_id = $1 AND surface = 'audit_chain'",
    )
    .bind(&env.tenant)
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(rows.len(), 1);
    let (rec, rec_seq) = &rows[0];
    assert_eq!(rec["context"]["record_type"], "ai.brutor.audit-chain-head");
    assert_eq!(rec["context"]["chain_id"], chain_id.as_str());
    assert_eq!(rec["context"]["seq_end"], seq + 5);
    assert_eq!(rec["kind"], "fyi");
    assert_eq!(rec["references"][0]["digest"], head_hash.as_str());
    assert!(
        !rec.to_string().contains(&"b".repeat(128)),
        "signature enters as a digest"
    );
    // The head that triggered it covers it.
    assert!(*rec_seq < body["head"]["tree_size"].as_i64().unwrap());

    // Sealed once: the next head does not repeat it.
    seal(&env, json!([{"record_type": "ai.brutor.report"}])).await;
    let (_, again) = call(&env, "POST", &path, None).await;
    assert_eq!(again["issued"], true);
    let (n,): (i64,) = sqlx::query_as(
        "SELECT count(*) FROM action_records WHERE tenant_id = $1 AND surface = 'audit_chain'",
    )
    .bind(&env.tenant)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(n, 1);
}

// ---------------------------------------------------------------------------
// Optional AAC profile export
// ---------------------------------------------------------------------------

/// Only this test touches `BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED`, and it drives both
/// states in sequence, so the process-wide flag cannot race another test.
#[sqlx::test(migrations = "./tests/migrations")]
async fn aac_export_is_flag_gated_and_reproduces_the_capsule_id(pool: PgPool) {
    let env = setup(&pool).await;
    let (st, body) = seal(
        &env,
        json!([{"record_type": "ai.brutor.declaration", "context": {"framework_id": "eu-ai-act"}}]),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{body}");
    let record_id = body["sealed"][0]["record_id"].as_str().unwrap().to_owned();
    let path = format!("/v1/evidence/records/{record_id}/aac");

    unsafe { std::env::remove_var("BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED") };
    brutor_gateway_core::env_vars::reload_for_tests();
    let (st, _) = call(&env, "GET", &path, None).await;
    assert_eq!(
        st,
        StatusCode::NOT_FOUND,
        "profile off ⇒ the route does not exist"
    );

    unsafe { std::env::set_var("BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED", "1") };
    brutor_gateway_core::env_vars::reload_for_tests();
    let (st, _) = call_as(&env, "GET", &path, None, None).await;
    assert_eq!(st, StatusCode::UNAUTHORIZED, "system callers only");
    let (st, out) = call(&env, "GET", &path, None).await;
    let (st_bad, _) = call(&env, "GET", "/v1/evidence/records/not-hex/aac", None).await;
    unsafe { std::env::remove_var("BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED") };
    brutor_gateway_core::env_vars::reload_for_tests();

    assert_eq!(st, StatusCode::OK, "{out}");
    assert_eq!(st_bad, StatusCode::BAD_REQUEST);
    assert_eq!(out["record_id"], record_id.as_str());
    let capsule_id = brutor_aac::canon::compute_capsule_id(&out["capsule"]).unwrap();
    assert_eq!(
        out["capsule_id"],
        capsule_id.as_str(),
        "the capsule id recomputes"
    );
    assert!(out["lossy"].as_array().is_some_and(|l| !l.is_empty()));
    assert!(
        !out["producer_envelope"].is_null(),
        "evidence key is set ⇒ envelope signed"
    );
}

// ---------------------------------------------------------------------------
// Log identity survives a changed deployment id
// ---------------------------------------------------------------------------

/// A log's id is fixed when it is created. A tenant whose log was created
/// under another `BRUTOR_DEPLOYMENT_ID` (a fresh trial download over an old
/// database volume) must keep appending to that log — before the fix every
/// seal failed on the unique `evidence_logs.tenant_id`. Simulated by
/// creating the log under a foreign id rather than mutating the
/// process-wide env var, which other tests read concurrently.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_log_keeps_its_id_when_the_deployment_id_changes(pool: PgPool) {
    let env = setup(&pool).await;
    let foreign = "f".repeat(64);
    assert_ne!(foreign, key::log_id_for(&env.tenant));
    sqlx::query(
        "INSERT INTO evidence_logs (log_id, tenant_id, next_seq, tree_size, kid) \
         VALUES ($1, $2, 0, 0, '')",
    )
    .bind(&foreign)
    .bind(&env.tenant)
    .execute(&pool)
    .await
    .unwrap();

    let (st, body) = seal(&env, json!([{"record_type": "ai.brutor.report"}])).await;
    assert_eq!(
        st,
        StatusCode::OK,
        "sealing must not fail on a drifted id: {body}"
    );
    assert_eq!(body["sealed"][0]["log_id"], foreign.as_str());

    let (st, head) = call(
        &env,
        "POST",
        &format!("/v1/evidence/logs/{}/tree-head", env.tenant),
        None,
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{head}");
    assert_eq!(head["issued"], true);

    let (st, bundle) = call(
        &env,
        "POST",
        "/v1/evidence/bundles",
        Some(json!({
            "tenant_id": env.tenant,
            "selector": {"record_ids": [body["sealed"][0]["record_id"]]},
            "payloads": "none",
        })),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{bundle}");
    assert_eq!(bundle["bundle"]["manifest"]["log_id"], foreign.as_str());
    let (st, v) = call(
        &env,
        "POST",
        "/v1/evidence/verify/bundle",
        Some(json!({"bundle": bundle["bundle"]})),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(v["ok"], true, "{v}");

    let (logs,): (i64,) = sqlx::query_as("SELECT count(*) FROM evidence_logs WHERE tenant_id = $1")
        .bind(&env.tenant)
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(logs, 1, "no second log is created");
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `background_writer` chain-column round-trip.
//!
//! The `INSERT INTO proxy_logs` statement in
//! `services/background_writer.rs` is the only writer of the six
//! chain columns (`root_task_id`, `current_task_id`,
//! `parent_task_id`, `delegation_depth`, `trace_root_kind`,
//! `trace_continuity`). A bind-position bug would silently corrupt
//! every audit row across LLM/MCP/Skill/A2A — there's no
//! schema-level guard against, say, writing root_kind into the
//! continuity column.
//!
//! This test schedules a write through the production
//! `BackgroundWriter` and then reads back the row to assert each
//! chain field landed in the correct column. Covers both the root
//! (depth=0, parent NULL) and child (depth=N, parent set) cases.

use std::time::Duration;

use sqlx::PgPool;
use ulid::Ulid;

use brutor_gateway_core::models::auth::{AuthContext, AuthType};
use brutor_gateway_core::services::a2a_delegation::{CallChain, TraceContinuity};
use brutor_gateway_core::services::background_writer::BackgroundWriter;
use brutor_gateway_core::services::proxy_logger;

use crate::common;

/// (id, chain_seq, prev_hash, row_hash) as read back from proxy_logs.
type ChainRow = (String, Option<i64>, Option<String>, Option<String>);

pub(crate) fn test_auth(tenant: &str) -> AuthContext {
    AuthContext {
        credential_kind: brutor_gateway_core::models::auth::CredentialKind::ApiKey,
        credential_assurance: Some(brutor_gateway_core::models::auth::CredentialAssurance::Key),
        credential_ref: None,
        credential_detail: Default::default(),
        identity_verdict: None,
        auth_type: AuthType::ApiKey,
        tenant_id: tenant.to_string(),
        gateway_user_id: None,
        gateway_username: None,
        gateway_tenant_id: None,
        idp_user_id: None,
        idp_username: None,
        idp_email: None,
        api_key_id: None,
        api_key_name: None,
        api_key_scope: None,
        api_key_group_id: None,
        api_key_is_platform: false,
        api_key_user_id: None,
        allowed_skill_ids: None,
        session_key: None,
        client_ip: None,
        resolved_group_id: None,
        actor_agent_id: None,
        session_token_type: None,
        agent_impl: Default::default(),
        admission_hold: None,
    }
}

/// Build a minimal ProxyLogInsert + apply_call_chain, schedule it
/// via the writer, then poll proxy_logs until the row appears or
/// we time out. Returns the populated row as a JSON value so each
/// caller can assert on whichever columns it cares about.
async fn write_and_read_back(
    pool: &PgPool,
    chain: &CallChain,
    current: &str,
    continuity: TraceContinuity,
    root_kind: Option<&str>,
) -> serde_json::Value {
    let auth = test_auth("tenant-bw");
    let mut log = proxy_logger::build_proxy_log(
        "llm",
        &auth,
        "POST",
        "/v1/chat/completions",
        None,
        None,
        200,
        11.0,
        true,
        None,
        &format!("corr-{}", Ulid::new()),
    );
    proxy_logger::apply_call_chain(&mut log, chain, current, continuity, root_kind);
    let log_id: String = log.id.clone();

    let writer = BackgroundWriter::new();
    writer.write_proxy_log(pool.clone(), log);

    // Poll up to ~2s for the spawned write task to land. Real
    // production traffic doesn't need this — but the test runs
    // concurrent with the spawned task so we have to wait.
    for _ in 0..40 {
        if let Ok(row) = sqlx::query_as::<_, (sqlx::types::Json<serde_json::Value>,)>(
            "SELECT to_jsonb(p.*) FROM proxy_logs p WHERE id = $1",
        )
        .bind(&log_id)
        .fetch_one(pool)
        .await
        {
            return row.0.0;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("background_writer never persisted proxy_logs row id={log_id}");
}

fn drop_pool_writer_stats() {
    // The writer's stats aren't shared across test invocations
    // (each `BackgroundWriter::new()` is fresh) so nothing to
    // reset between tests. Helper exists as a docstring anchor in
    // case a future refactor adds cross-test state.
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn writer_persists_root_chain_with_null_parent(pool: PgPool) {
    drop_pool_writer_stats();
    common::seed_tenant(&pool, "tenant-bw").await;

    let root = Ulid::new();
    let chain = CallChain::root(root);
    let current = Ulid::new().to_string();

    let row = write_and_read_back(
        &pool,
        &chain,
        &current,
        TraceContinuity::Minted,
        Some("llm_user"),
    )
    .await;

    // Root chain invariants: parent NULL, depth=0, root and
    // current_task_id stamped, continuity=minted, root_kind set.
    assert_eq!(
        row["root_task_id"].as_str(),
        Some(root.to_string().as_str())
    );
    assert_eq!(row["current_task_id"].as_str(), Some(current.as_str()));
    assert!(
        row["parent_task_id"].is_null(),
        "root chain must have NULL parent_task_id, got {}",
        row["parent_task_id"]
    );
    assert_eq!(row["delegation_depth"].as_i64(), Some(0));
    assert_eq!(row["trace_continuity"].as_str(), Some("minted"));
    assert_eq!(row["trace_root_kind"].as_str(), Some("llm_user"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn writer_persists_verified_child_chain(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-bw").await;

    let root = Ulid::new();
    let parent = Ulid::new();
    let chain = CallChain {
        root_task_id: root,
        parent_task_id: parent,
        depth: 3,
        actor_agent_id: None,
        subject: None,
    };
    let current = Ulid::new().to_string();

    let row = write_and_read_back(
        &pool,
        &chain,
        &current,
        TraceContinuity::Verified,
        Some("agent_invocation"),
    )
    .await;

    // Child chain: all four identity columns + depth + the two
    // new feature-1 columns must all be populated correctly.
    assert_eq!(
        row["root_task_id"].as_str(),
        Some(root.to_string().as_str())
    );
    assert_eq!(
        row["parent_task_id"].as_str(),
        Some(parent.to_string().as_str())
    );
    assert_eq!(row["current_task_id"].as_str(), Some(current.as_str()));
    assert_eq!(row["delegation_depth"].as_i64(), Some(3));
    assert_eq!(row["trace_continuity"].as_str(), Some("verified"));
    assert_eq!(row["trace_root_kind"].as_str(), Some("agent_invocation"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn writer_persists_each_continuity_string(pool: PgPool) {
    // Each TraceContinuity variant rides on the same INSERT
    // binding ($77) — if a future refactor swaps the bind order
    // of $77 and $76 (root_kind), the strings would land in
    // each other's column. Pin every variant.
    common::seed_tenant(&pool, "tenant-bw").await;

    for (cont, expected) in [
        (TraceContinuity::Minted, "minted"),
        (TraceContinuity::Verified, "verified"),
        (TraceContinuity::ClientDropped, "client_dropped"),
        (TraceContinuity::SigFailed, "sig_failed"),
    ] {
        let row = write_and_read_back(
            &pool,
            &CallChain::root(Ulid::new()),
            &Ulid::new().to_string(),
            cont,
            Some("external_api"),
        )
        .await;
        assert_eq!(
            row["trace_continuity"].as_str(),
            Some(expected),
            "continuity {cont:?} → wire string {expected}",
        );
        // root_kind must remain "external_api" in EVERY iteration
        // — that's the smoking-gun test for bind-position swap
        // between $76 and $77.
        assert_eq!(
            row["trace_root_kind"].as_str(),
            Some("external_api"),
            "root_kind must not be clobbered by continuity column"
        );
    }
}

/// Batching round-trip: schedule MORE rows than `MAX_BATCH_ROWS` (500) through
/// a single writer and assert every one persists. This exercises the multi-row
/// INSERT path across batch boundaries — a bind-count or batch-loop bug would
/// drop or corrupt rows once a batch fills.
#[sqlx::test(migrations = "./tests/migrations")]
async fn writer_batches_beyond_max_batch_rows(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-bw").await;

    let writer = BackgroundWriter::new();
    let total: i64 = 1200; // > 2 × MAX_BATCH_ROWS

    for _ in 0..total {
        let auth = test_auth("tenant-bw");
        let log = proxy_logger::build_proxy_log(
            "llm",
            &auth,
            "POST",
            "/v1/chat/completions",
            None,
            None,
            200,
            7.0,
            true,
            None,
            &format!("corr-{}", Ulid::new()),
        );
        writer.write_proxy_log(pool.clone(), log);
    }

    // Poll until all rows land (consumer drains in batches of up to 500).
    // Generous window: on a loaded machine the drain takes >5s even though
    // it finishes in ~1s when idle — the assertion is completeness, not speed.
    let mut persisted = 0i64;
    for _ in 0..600 {
        persisted =
            sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM proxy_logs WHERE tenant_id = $1")
                .bind("tenant-bw")
                .fetch_one(&pool)
                .await
                .expect("count proxy_logs");
        if persisted >= total {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }

    assert_eq!(
        persisted, total,
        "all {total} batched rows must persist, saw {persisted}"
    );

    // No drops expected — channel capacity (20k) comfortably exceeds the load.
    assert_eq!(
        writer
            .stats()
            .total_dropped
            .load(std::sync::atomic::Ordering::Relaxed),
        0,
        "no rows should be dropped at this volume"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn writer_persists_null_root_kind_explicitly(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-bw").await;

    let row = write_and_read_back(
        &pool,
        &CallChain::root(Ulid::new()),
        &Ulid::new().to_string(),
        TraceContinuity::Minted,
        None,
    )
    .await;
    // NULL propagation — without this assertion a wrong default
    // string (e.g. "unknown") would silently land in the column.
    assert!(
        row["trace_root_kind"].is_null(),
        "None root_kind must persist as SQL NULL, got {}",
        row["trace_root_kind"]
    );
}

// ---------------------------------------------------------------------------
// Decision record: the Minimum Action-Evidence Bundle.
//
// These eight columns exist so the audit trail can answer *which* policy
// decided, under *what* authority, via *which* exception path, against
// *which* upstream call — questions the previous booleans could not.
//
// The failure mode this guards is silent: add a column to the struct, forget
// it in the INSERT column list or bind order, and every row persists with a
// NULL that reads exactly like "no decision was taken". Nothing errors. The
// evidence is simply absent, which is the one thing an assurance product
// cannot afford to be wrong about.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn writer_persists_every_decision_record_field(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-bw").await;

    let auth = test_auth("tenant-bw");
    let mut log = proxy_logger::build_proxy_log(
        "llm",
        &auth,
        "POST",
        "/v1/chat/completions",
        None,
        None,
        403,
        7.0,
        false,
        Some("blocked"),
        &format!("corr-{}", Ulid::new()),
    );

    log.argument_policy_id = Some("argpol-42".to_string());
    log.guardrail_config_id = Some("gr-cfg-7".to_string());
    log.guardrail_check = Some("pii".to_string());
    log.guardrail_action = Some("redact".to_string());
    log.autonomy_level = Some("restricted".to_string());
    log.autonomy_provenance = Some("last_known_good".to_string());
    log.consumed_approval_request_id = Some("appr-99".to_string());
    log.upstream_request_id = Some("req_upstream_abc123".to_string());
    log.pre_action_state = Some(serde_json::json!({
        "kind": "llm_quota",
        "scope": "resource_group",
        "daily_cost": 12.5,
    }));

    let log_id = log.id.clone();
    let writer = BackgroundWriter::new();
    writer.write_proxy_log(pool.clone(), log);

    let mut row = None;
    for _ in 0..40 {
        if let Ok(r) = sqlx::query_as::<_, (sqlx::types::Json<serde_json::Value>,)>(
            "SELECT to_jsonb(p.*) FROM proxy_logs p WHERE id = $1",
        )
        .bind(&log_id)
        .fetch_one(&pool)
        .await
        {
            row = Some(r.0.0);
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let row = row.expect("background_writer never persisted the decision-record row");

    for (column, expected) in [
        ("argument_policy_id", "argpol-42"),
        ("guardrail_config_id", "gr-cfg-7"),
        ("guardrail_check", "pii"),
        ("guardrail_action", "redact"),
        ("autonomy_level", "restricted"),
        ("autonomy_provenance", "last_known_good"),
        ("consumed_approval_request_id", "appr-99"),
        ("upstream_request_id", "req_upstream_abc123"),
    ] {
        assert_eq!(
            row[column].as_str(),
            Some(expected),
            "proxy_logs.{column} did not round-trip — check the INSERT column \
             list and bind order in background_writer::insert_logs",
        );
    }

    // JSONB, so it round-trips as a document rather than a string.
    assert_eq!(
        row["pre_action_state"]["kind"].as_str(),
        Some("llm_quota"),
        "proxy_logs.pre_action_state did not round-trip",
    );
    assert_eq!(row["pre_action_state"]["daily_cost"].as_f64(), Some(12.5));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_absent_decision_is_null_not_empty(pool: PgPool) {
    // NULL has to mean "no such decision was taken on this call". If the
    // writer ever bound empty strings instead, "no guardrail fired" and
    // "a guardrail with a blank id fired" would be indistinguishable, and
    // the assurance report's not-covered section would under-report.
    common::seed_tenant(&pool, "tenant-bw").await;

    let root = Ulid::new();
    let chain = CallChain::root(root);
    let row = write_and_read_back(
        &pool,
        &chain,
        &Ulid::new().to_string(),
        TraceContinuity::Minted,
        Some("llm_user"),
    )
    .await;

    for column in [
        "argument_policy_id",
        "guardrail_config_id",
        "guardrail_check",
        "guardrail_action",
        "autonomy_level",
        "autonomy_provenance",
        "consumed_approval_request_id",
        "upstream_request_id",
        "pre_action_state",
    ] {
        assert!(
            row[column].is_null(),
            "proxy_logs.{column} must be NULL when no such decision was taken, got {:?}",
            row[column],
        );
    }
}

// ---------------------------------------------------------------------------
// Tamper evidence.
//
// The unit tests cover the hashing rules against fixtures. What only a live
// database proves is that the chain survives the write path: that the columns
// are in the INSERT, that sequence numbers land dense, and that the links
// stored on disk actually reference each other.
//
// The failure this guards is silent in the worst way. Miss a column in the
// INSERT and every row persists with a NULL hash — nothing errors, the rows
// look complete, and the tamper-evidence property simply is not there.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_hash_chain_survives_the_write_path(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-bw").await;

    let writer = BackgroundWriter::new();
    let mut ids = Vec::new();
    for _ in 0..3 {
        let auth = test_auth("tenant-bw");
        let log = proxy_logger::build_proxy_log(
            "llm",
            &auth,
            "POST",
            "/v1/chat/completions",
            None,
            None,
            200,
            5.0,
            true,
            None,
            &format!("corr-{}", Ulid::new()),
        );
        ids.push(log.id.clone());
        writer.write_proxy_log(pool.clone(), log);
    }

    // Poll for all three.
    let mut rows: Vec<ChainRow> = Vec::new();
    for _ in 0..40 {
        rows = sqlx::query_as(
            "SELECT id, chain_seq, prev_hash, row_hash FROM proxy_logs \
             WHERE tenant_id = 'tenant-bw' AND chain_seq IS NOT NULL \
             ORDER BY chain_seq",
        )
        .fetch_all(&pool)
        .await
        .expect("query");
        if rows.len() >= 3 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(
        rows.len() >= 3,
        "expected 3 chained rows, got {} — are the chain columns in the INSERT?",
        rows.len(),
    );

    for (id, seq, prev, hash) in &rows {
        assert!(seq.is_some(), "row {id} persisted without a chain_seq");
        assert!(
            prev.as_ref().is_some_and(|h| h.len() == 64),
            "row {id} persisted without a 64-char prev_hash",
        );
        assert!(
            hash.as_ref().is_some_and(|h| h.len() == 64),
            "row {id} persisted without a 64-char row_hash",
        );
    }

    // Dense sequence, and each row's prev_hash is its predecessor's row_hash.
    for w in rows.windows(2) {
        assert_eq!(
            w[1].1.unwrap(),
            w[0].1.unwrap() + 1,
            "chain_seq must be dense on disk",
        );
        assert_eq!(
            w[1].2, w[0].3,
            "prev_hash must reference the preceding row's row_hash",
        );
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_chain_position_is_claimed_once(pool: PgPool) {
    // The partial unique index is what makes a gap mean "deleted" rather than
    // "maybe two writers collided". Without it, a duplicate sequence number
    // would be indistinguishable from a legitimate chain.
    common::seed_tenant(&pool, "tenant-bw").await;

    for attempt in 0..2 {
        let res = sqlx::query(
            "INSERT INTO proxy_logs \
             (id, tenant_id, timestamp, proxy_type, end_user_session_id, \
              http_method, endpoint, success, chain_id, chain_seq, prev_hash, row_hash) \
             VALUES ($1, 'tenant-bw', NOW(), 'llm', 'sess', 'POST', '/x', true, \
                     'chain-dup', 7, $2, $3)",
        )
        .bind(format!("dup-{attempt}"))
        .bind("0".repeat(64))
        .bind("a".repeat(64))
        .execute(&pool)
        .await;

        if attempt == 0 {
            assert!(res.is_ok(), "first claim of a chain position must succeed");
        } else {
            assert!(
                res.is_err(),
                "a second row must not be able to claim chain position 7",
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Content digests, trace context, tool provenance, deciding model.
// Same guard as above: a column the INSERT forgets persists as a NULL that
// reads exactly like "nothing to say".
// ---------------------------------------------------------------------------

async fn read_row_json(pool: &PgPool, id: &str) -> serde_json::Value {
    for _ in 0..60 {
        if let Ok(r) = sqlx::query_as::<_, (sqlx::types::Json<serde_json::Value>,)>(
            "SELECT to_jsonb(p.*) FROM proxy_logs p WHERE id = $1",
        )
        .bind(id)
        .fetch_one(pool)
        .await
        {
            return r.0.0;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("row {id} never landed");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn writer_persists_the_a171_columns_and_seals_content_digests(pool: PgPool) {
    use sha2::Digest as _;
    common::seed_tenant(&pool, "tenant-a171").await;
    let auth = test_auth("tenant-a171");
    let request =
        r#"{"method":"tools/call","params":{"name":"refund","arguments":{"order":"4711"}}}"#;
    let mut log = proxy_logger::build_proxy_log(
        "mcp",
        &auth,
        "POST",
        "mcp-proxy",
        None,
        Some("srv-1"),
        200,
        4.0,
        true,
        None,
        &format!("corr-{}", Ulid::new()),
    );
    log.request_body_preview = Some(request.to_string());
    log.response_body_preview =
        Some(r#"{"result":{"content":[{"type":"text","text":"done"}]}}"#.into());
    log.trace_id = Some("0af7651916cd43dd8448eb211c80319c".into());
    log.span_id = Some("b7ad6b7169203331".into());
    log.server_version = Some("2.4.1".into());
    log.tool_definition_hash = Some("c".repeat(64));
    let id = log.id.clone();
    BackgroundWriter::new().write_proxy_log(pool.clone(), log);

    let row = read_row_json(&pool, &id).await;
    let expected = hex::encode(sha2::Sha256::digest(request.as_bytes()));
    assert_eq!(
        row["request_content_hash"].as_str(),
        Some(expected.as_str())
    );
    assert_eq!(
        row["response_content_hash"].as_str().map(str::len),
        Some(64)
    );
    for (column, expected) in [
        ("trace_id", "0af7651916cd43dd8448eb211c80319c"),
        ("span_id", "b7ad6b7169203331"),
        ("server_version", "2.4.1"),
        (
            "tool_definition_hash",
            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        ),
    ] {
        assert_eq!(
            row[column].as_str(),
            Some(expected),
            "proxy_logs.{column} did not round-trip — check the INSERT column list and bind order",
        );
    }
    assert!(row["row_hash"].as_str().is_some());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_action_row_is_stamped_with_the_model_that_decided_it(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-decide").await;
    let auth = test_auth("tenant-decide");
    let writer = BackgroundWriter::new();

    // Ledger path: the model call landed in an earlier flush.
    let root = format!("run-{}", Ulid::new());
    let mut llm = proxy_logger::build_proxy_log(
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
    llm.root_task_id = Some(root.clone());
    llm.llm_model_used = Some("gpt-5.2".into());
    let llm_id = llm.id.clone();
    writer.write_proxy_log(pool.clone(), llm);
    let llm_row = read_row_json(&pool, &llm_id).await;
    assert!(
        llm_row["deciding_model"].is_null(),
        "a model call is not decided by a model"
    );

    let mut tool = proxy_logger::build_proxy_log(
        "mcp",
        &auth,
        "POST",
        "mcp-proxy",
        None,
        Some("srv-1"),
        200,
        2.0,
        true,
        None,
        &format!("corr-{}", Ulid::new()),
    );
    tool.root_task_id = Some(root.clone());
    let tool_id = tool.id.clone();
    writer.write_proxy_log(pool.clone(), tool);
    assert_eq!(
        read_row_json(&pool, &tool_id).await["deciding_model"].as_str(),
        Some("gpt-5.2")
    );

    // Batch path: model call and tool call queued back to back.
    let root2 = format!("run-{}", Ulid::new());
    let mut llm2 = proxy_logger::build_proxy_log(
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
    llm2.root_task_id = Some(root2.clone());
    llm2.llm_model_used = Some("claude-sonnet-5".into());
    let mut tool2 = proxy_logger::build_proxy_log(
        "skill",
        &auth,
        "POST",
        "/v1/proxy/skills/run",
        None,
        None,
        200,
        2.0,
        true,
        None,
        &format!("corr-{}", Ulid::new()),
    );
    tool2.root_task_id = Some(root2.clone());
    let tool2_id = tool2.id.clone();
    writer.write_proxy_log(pool.clone(), llm2);
    writer.write_proxy_log(pool.clone(), tool2);
    assert_eq!(
        read_row_json(&pool, &tool2_id).await["deciding_model"].as_str(),
        Some("claude-sonnet-5")
    );

    // No run, no answer — never a guess from some other run's model.
    let mut orphan = proxy_logger::build_proxy_log(
        "mcp",
        &auth,
        "POST",
        "mcp-proxy",
        None,
        Some("srv-1"),
        200,
        2.0,
        true,
        None,
        &format!("corr-{}", Ulid::new()),
    );
    orphan.root_task_id = None;
    let orphan_id = orphan.id.clone();
    writer.write_proxy_log(pool.clone(), orphan);
    assert!(read_row_json(&pool, &orphan_id).await["deciding_model"].is_null());
}

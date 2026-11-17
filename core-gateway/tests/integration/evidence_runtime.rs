// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every verdict sealed, at the surfaces that are easy
//! to miss:
//!
//! * an A2A v1 refusal now writes a row and so a `denied` record;
//! * an approval that lapses seals `expired`, chained `supersedes` to the
//!   `awaiting_human` record raised when it was enqueued;
//! * a two-phase system seals `planned` before dispatch and the outcome
//!   `confirms` it under the same `action_id`, in log order;
//! * the portal notice endpoint seals `ai.brutor.notice` (and refuses a
//!   handle that is not a ULID), and an inference record for that
//!   conversation cites it;
//! * LLM completions are labelled `X-Brutor-AI-Generated: true`;
//! * a skill held by its execution mode is `awaiting_human` keyed by its
//!   approval request and its lapse seals `expired`, chained `supersedes`
//!   (the approved run: `approval_two_holds`);
//! * a read-only tool seals only on a sensitive resource group.

use std::time::Duration;

use axum::body::Body;
use axum::http::StatusCode;
use brutor_gateway_core::models::db::ProxyLogInsert;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::background_writer::BackgroundWriter;
use brutor_gateway_core::services::evidence::{planned, request};
use brutor_gateway_core::services::proxy_logger;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use ulid::Ulid;

use crate::background_writer_chain::test_auth;
use crate::common;

pub(crate) const SEED: [u8; 32] = [0x42; 32];

pub(crate) fn enable_evidence() {
    // Process-global; the same values `evidence_records` sets, so the two
    // modules agree whatever order the harness runs them in.
    unsafe {
        std::env::set_var("BRUTOR_EVIDENCE_SIGNING_KEY", hex::encode(SEED));
        std::env::set_var("BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES", "2");
        std::env::set_var("BRUTOR_EVIDENCE_WITNESS_ENABLED", "off");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
}

pub(crate) async fn body_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

/// Poll until `sql` (one bound text parameter) returns a row.
pub(crate) async fn wait_for_row<T>(pool: &PgPool, sql: &str, bind: &str) -> T
where
    T: for<'r> sqlx::FromRow<'r, sqlx::postgres::PgRow> + Send + Unpin,
{
    for _ in 0..120 {
        if let Some(r) = sqlx::query_as::<_, T>(sql)
            .bind(bind)
            .fetch_optional(pool)
            .await
            .unwrap()
        {
            return r;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("timed out waiting for: {sql} [{bind}]");
}

pub(crate) async fn records(pool: &PgPool, tenant: &str) -> Vec<(String, i64, String, Value)> {
    sqlx::query_as(
        "SELECT record_id, seq, verdict, record FROM action_records \
         WHERE tenant_id = $1 ORDER BY seq",
    )
    .bind(tenant)
    .fetch_all(pool)
    .await
    .unwrap()
}

pub(crate) fn assert_verifies(record: &Value) {
    let v = brutor_evidence::verify(record, None);
    assert!(v.ok, "{:?}", v.findings);
}

/// An AI System resource group with a compliance profile `core`.
pub(crate) async fn seed_ai_system(pool: &PgPool, tenant: &str, id: &str, core: Value) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, \
         name, is_active, is_system, group_type, system_kind) \
         VALUES ($1, $2, 0, false, $1, true, false, 'AI_SYSTEM', 'agent') \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .execute(pool)
    .await
    .expect("seed ai system");
    sqlx::query(
        "INSERT INTO ai_system_compliance_profiles (ai_system_id, tenant_id, core, frameworks) \
         VALUES ($1, $2, $3, '{}'::jsonb)",
    )
    .bind(id)
    .bind(tenant)
    .bind(core)
    .execute(pool)
    .await
    .expect("seed compliance profile");
}

// ---------------------------------------------------------------------------
// A2A v1 refusals
// ---------------------------------------------------------------------------

/// The user of the agent's key. The test asserts this id is nowhere in the
/// sealed record, by substring, and the record carries hashes, signatures
/// and timestamps: a four-digit id (it was 7801) turned up in one of those
/// by chance about once in a few hundred runs. Nine digits do not.
const AGENT_KEY_USER: i32 = 780_100_417;
#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_v1_agent_refusal_writes_a_row_and_a_denied_record(pool: PgPool) {
    enable_evidence();
    let t = "tenant-ev-a2a";
    common::seed_tenant(&pool, t).await;
    common::seed_resource_group(&pool, "rg-a2a", t).await;
    common::seed_end_user(&pool, AGENT_KEY_USER, t).await;
    common::add_direct_membership(&pool, AGENT_KEY_USER, "rg-a2a", t).await;
    common::seed_agent_card(&pool, "card-bill", t, "billing-agent").await;
    common::bind_agent_card_to_rg(&pool, "card-bill", "rg-a2a").await;
    let api_key = common::seed_api_key(&pool, t, "rg-a2a", Some(AGENT_KEY_USER), "agent-key").await;
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-ev', $1, 'agent-ev', 'manual', true, 'active', true)",
    )
    .bind(t)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "UPDATE api_keys SET agent_id = 'agent-ev' WHERE tenant_id = $1 AND name = 'agent-key'",
    )
    .bind(t)
    .execute(&pool)
    .await
    .unwrap();

    let app = common::build_a2a_v1_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        &format!("/a2a/{t}/billing-agent/message:send"),
        json!({"message": {"messageId": "01msg", "role": "ROLE_USER",
                           "parts": [{"text": "refund order #1"}]}}),
        Some(&api_key),
        Some(t),
    );
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);

    let (subtype, record_id): (Option<String>, Option<String>) = wait_for_row(
        &pool,
        "SELECT proxy_subtype, record_id FROM proxy_logs \
         WHERE tenant_id = $1 AND proxy_type = 'a2a' AND record_id IS NOT NULL",
        t,
    )
    .await;
    assert_eq!(subtype.as_deref(), Some("agent_authz_block"));
    let recs = records(&pool, t).await;
    let (rid, _, verdict, record) = recs
        .iter()
        .find(|r| Some(&r.0) == record_id.as_ref())
        .expect("the row's record is in the log");
    assert_eq!(verdict, "denied");
    assert_eq!(
        record["action_id"],
        format!(
            "a2a/{}",
            record["context"]["proxy_log_id"].as_str().unwrap()
        )
    );
    assert!(record.get("effect").is_none());
    assert_eq!(record["disposition"]["approver"], "policy");
    assert!(record.to_string().contains("ai.brutor.agent_grant"));
    assert!(
        !record.to_string().contains(&AGENT_KEY_USER.to_string()),
        "never-enters: no user id"
    );
    assert_verifies(record);
    assert_eq!(rid.len(), 64);
}

/// A card an operator disabled or revoked refuses an authenticated caller:
/// a verdict, so a row and a `denied` record (control `ai.brutor.access`).
/// An unknown card (pre-verdict) still writes nothing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_v1_disabled_or_revoked_card_refusal_is_sealed_denied(pool: PgPool) {
    enable_evidence();
    let t = "tenant-ev-a2a-off";
    common::seed_tenant(&pool, t).await;
    common::seed_resource_group(&pool, "rg-a2a-off", t).await;
    common::seed_end_user(&pool, 7803, t).await;
    common::add_direct_membership(&pool, 7803, "rg-a2a-off", t).await;
    common::seed_agent_card(&pool, "card-off", t, "off-agent").await;
    common::seed_agent_card(&pool, "card-rev", t, "revoked-agent").await;
    common::bind_agent_card_to_rg(&pool, "card-off", "rg-a2a-off").await;
    common::bind_agent_card_to_rg(&pool, "card-rev", "rg-a2a-off").await;
    sqlx::query("UPDATE agent_cards SET enabled = false WHERE id = 'card-off'")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("UPDATE agent_cards SET revoked_at = NOW() WHERE id = 'card-rev'")
        .execute(&pool)
        .await
        .unwrap();
    let api_key = common::seed_api_key(&pool, t, "rg-a2a-off", Some(7803), "off-key").await;
    let app = common::build_a2a_v1_app(common::build_test_state(pool.clone()));

    for (card_name, card_id) in [("off-agent", "card-off"), ("revoked-agent", "card-rev")] {
        let req = common::build_post_request(
            &format!("/a2a/{t}/{card_name}/message:send"),
            json!({"message": {"messageId": "01msg", "role": "ROLE_USER",
                               "parts": [{"text": "hello"}]}}),
            Some(&api_key),
            Some(t),
        );
        let resp = app.clone().oneshot(req).await.unwrap();
        assert!(resp.status().is_client_error(), "{}", resp.status());
        let (subtype, record_id): (Option<String>, Option<String>) = wait_for_row(
            &pool,
            "SELECT proxy_subtype, record_id FROM proxy_logs \
             WHERE server_id = $1 AND proxy_type = 'a2a' AND record_id IS NOT NULL",
            card_id,
        )
        .await;
        assert_eq!(subtype.as_deref(), Some("access_denied"));
        let recs = records(&pool, t).await;
        let rec = &recs
            .iter()
            .find(|r| Some(&r.0) == record_id.as_ref())
            .expect("the refusal's record")
            .3;
        assert_eq!(rec["verdict"], "denied");
        assert_eq!(rec["disposition"]["decision"], "reject");
        assert!(rec.get("effect").is_none());
        assert!(rec.to_string().contains("ai.brutor.access"));
        assert_verifies(rec);
    }

    // Unknown card: decided before there is a target — no row.
    let req = common::build_post_request(
        &format!("/a2a/{t}/no-such-agent/message:send"),
        json!({"message": {"messageId": "01msg", "role": "ROLE_USER",
                           "parts": [{"text": "hello"}]}}),
        Some(&api_key),
        Some(t),
    );
    let _ = app.clone().oneshot(req).await.unwrap();
    tokio::time::sleep(Duration::from_millis(300)).await;
    let (n,): (i64,) = sqlx::query_as(
        "SELECT count(*) FROM proxy_logs WHERE tenant_id = $1 AND proxy_type = 'a2a'",
    )
    .bind(t)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(n, 2, "only the two card refusals wrote rows");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a2a_v1_autonomy_refusal_of_a_plain_key_is_sealed_denied(pool: PgPool) {
    enable_evidence();
    let t = "tenant-ev-a2a-aut";
    common::seed_tenant(&pool, t).await;
    seed_ai_system(&pool, t, "sys-susp", json!({})).await;
    sqlx::query("UPDATE resource_groups SET autonomy_level = 'suspended' WHERE id = 'sys-susp'")
        .execute(&pool)
        .await
        .unwrap();
    common::seed_end_user(&pool, 7802, t).await;
    common::add_direct_membership(&pool, 7802, "sys-susp", t).await;
    common::seed_agent_card(&pool, "card-aut", t, "ops-agent").await;
    common::bind_agent_card_to_rg(&pool, "card-aut", "sys-susp").await;
    let api_key = common::seed_api_key(&pool, t, "sys-susp", Some(7802), "plain-key").await;

    let app = common::build_a2a_v1_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        &format!("/a2a/{t}/ops-agent/message:send"),
        json!({"message": {"messageId": "01msg", "role": "ROLE_USER",
                           "parts": [{"text": "restart"}]}}),
        Some(&api_key),
        Some(t),
    );
    let resp = app.oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    let (subtype, _rid): (Option<String>, Option<String>) = wait_for_row(
        &pool,
        "SELECT proxy_subtype, record_id FROM proxy_logs \
         WHERE tenant_id = $1 AND proxy_type = 'a2a' AND record_id IS NOT NULL",
        t,
    )
    .await;
    assert_eq!(subtype.as_deref(), Some("autonomy_block"));
    let recs = records(&pool, t).await;
    let denied = recs
        .iter()
        .find(|r| r.2 == "denied")
        .expect("denied record");
    assert_eq!(denied.3["system"]["ai_system_id"], "sys-susp");
    assert!(denied.3.to_string().contains("ai.brutor.autonomy"));
    assert_verifies(&denied.3);
}

// ---------------------------------------------------------------------------
// Approval expiry
// ---------------------------------------------------------------------------

pub(crate) fn approval_row(tenant: &str, approval_id: &str) -> ProxyLogInsert {
    let mut log = proxy_logger::build_proxy_log(
        "mcp",
        &test_auth(tenant),
        "POST",
        "/v1/proxy/mcp/srv-1",
        None,
        Some("srv-1"),
        202,
        4.0,
        false,
        Some("approval_required"),
        &format!("corr-{}", Ulid::new()),
    );
    log.mcp_method = Some("tools/call".into());
    log.mcp_capability_name = Some("send_payment".into());
    log.proxy_subtype = Some("approval_required".into());
    log.custom_metadata = Some(json!({"approval_request_id": approval_id}));
    log
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approval_expiry_seals_expired_superseding_the_awaiting_record(pool: PgPool) {
    enable_evidence();
    let t = "tenant-ev-apr";
    common::seed_tenant(&pool, t).await;
    let apr = format!("apr-{}", Ulid::new()).to_lowercase();

    // Enqueue: the approval_required row seals `awaiting_human`.
    let writer = BackgroundWriter::new();
    writer.write_proxy_log(pool.clone(), approval_row(t, &apr));
    let (awaiting_id,): (String,) = wait_for_row(
        &pool,
        "SELECT record_id FROM action_records WHERE tenant_id = $1 AND verdict = 'awaiting_human'",
        t,
    )
    .await;

    // The request lapses.
    sqlx::query(
        "INSERT INTO tool_approval_requests \
         (id, tenant_id, server_config_id, server_name, capability_type, capability_name, \
          requester_type, requester_group_id, status, timeout_seconds, expires_at, created_at) \
         VALUES ($1, $2, 'srv-1', 'payments', 'tool', 'send_payment', 'user', 'rg-1', \
                 'pending', 60, NOW() - INTERVAL '5 minutes', NOW() - INTERVAL '6 minutes')",
    )
    .bind(&apr)
    .bind(t)
    .execute(&pool)
    .await
    .unwrap();
    let n = routes::approvals::expire_pending_approvals(&pool)
        .await
        .unwrap();
    assert!(n >= 1);

    let recs = records(&pool, t).await;
    let expired = recs
        .iter()
        .find(|r| r.2 == "expired")
        .expect("an expired record was sealed");
    let rec = &expired.3;
    assert_eq!(rec["context"]["record_type"], "ai.brutor.approval-expired");
    assert_eq!(rec["context"]["approval_request_id"], apr.as_str());
    assert_eq!(rec["disposition"]["decision"], "deferred");
    assert_eq!(rec["disposition"]["approver"], "policy");
    assert_eq!(rec["disposition"]["human_disposed"], false);
    assert_eq!(rec["chain"]["relation"], "supersedes");
    assert_eq!(rec["chain"]["parent_record_id"], awaiting_id.as_str());
    let awaiting_seq = recs.iter().find(|r| r.0 == awaiting_id).unwrap().1;
    assert!(
        expired.1 > awaiting_seq,
        "the expiry lands after what it supersedes"
    );
    assert_verifies(rec);

    // A second sweep finds nothing pending and seals nothing more.
    routes::approvals::expire_pending_approvals(&pool)
        .await
        .unwrap();
    let again = records(&pool, t).await;
    assert_eq!(again.iter().filter(|r| r.2 == "expired").count(), 1);
}

// ---------------------------------------------------------------------------
// A skill held by its execution mode (a hold in the approval queue)
// ---------------------------------------------------------------------------

/// Call a skill whose group states `execution_mode: approval_required`
/// through the REST route. Returns (approval request id, `awaiting_human`
/// record id).
async fn hold_skill_execution(pool: &PgPool, t: &str, rg: &str, user: i32) -> (String, String) {
    common::seed_tenant(pool, t).await;
    common::seed_resource_group(pool, rg, t).await;
    common::seed_end_user(pool, user, t).await;
    common::add_direct_membership(pool, user, rg, t).await;
    let skill = format!("sk-{rg}");
    common::seed_agent_skill(pool, &skill, t, "payout-runner", "# Payout", "Runs payouts").await;
    common::bind_skill_to_rg(pool, &skill, rg).await;
    common::set_rg_skill_governance(
        pool,
        rg,
        json!({"execution_mode": "approval_required", "approval_timeout_seconds": 120}),
    )
    .await;
    let api_key = common::seed_api_key(pool, t, rg, Some(user), &format!("k-{rg}")).await;
    let app = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let resp = app
        .oneshot(common::build_post_request(
            "/v1/proxy/skills/execute",
            json!({"skill_id": skill, "input_params": {"amount": "10"}}),
            Some(&api_key),
            Some(t),
        ))
        .await
        .unwrap();
    let status = resp.status();
    let body = body_json(resp.into_body()).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let approval = body["approval_request_id"]
        .as_str()
        .unwrap_or_else(|| panic!("the call is held: {body}"))
        .to_string();

    // The held call seals `awaiting_human`, keyed by the approval request.
    let (awaiting_id,): (String,) = wait_for_row(
        pool,
        "SELECT record_id FROM action_records WHERE verdict = 'awaiting_human' \
           AND record->'context'->>'approval_request_id' = $1",
        &approval,
    )
    .await;
    let (status, expires): (String, chrono::DateTime<chrono::Utc>) =
        sqlx::query_as("SELECT status, expires_at FROM tool_approval_requests WHERE id = $1")
            .bind(&approval)
            .fetch_one(pool)
            .await
            .unwrap();
    assert_eq!(status, "pending");
    let left = (expires - crate::common::db_now()).num_seconds();
    assert!(
        (100..=121).contains(&left),
        "group window honoured: {left}s"
    );
    let (executions,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM agent_skill_executions WHERE tenant_id = $1")
            .bind(t)
            .fetch_one(pool)
            .await
            .unwrap();
    assert_eq!(executions, 0, "a held call is not an execution");
    (approval, awaiting_id)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_held_skills_approval_expiry_seals_expired_superseding_awaiting(pool: PgPool) {
    enable_evidence();
    let t = "tenant-ev-skx";
    let (approval, awaiting_id) = hold_skill_execution(&pool, t, "rg-ev-skx", 7811).await;

    let recs = records(&pool, t).await;
    let awaiting = &recs.iter().find(|r| r.0 == awaiting_id).unwrap().3;
    assert_eq!(awaiting["context"]["surface"], "skill");
    assert_eq!(awaiting["disposition"]["decision"], "needs_input");
    assert_verifies(awaiting);

    // Nothing lapses before the deadline.
    assert_eq!(
        routes::approvals::expire_pending_approvals(&pool)
            .await
            .unwrap(),
        0
    );
    sqlx::query(
        "UPDATE tool_approval_requests SET expires_at = NOW() - INTERVAL '1 minute' WHERE id = $1",
    )
    .bind(&approval)
    .execute(&pool)
    .await
    .unwrap();
    assert_eq!(
        routes::approvals::expire_pending_approvals(&pool)
            .await
            .unwrap(),
        1
    );

    let (status,): (String,) =
        sqlx::query_as("SELECT status FROM tool_approval_requests WHERE id = $1")
            .bind(&approval)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(status, "expired");
    let recs = records(&pool, t).await;
    let expired = recs
        .iter()
        .find(|r| r.2 == "expired")
        .expect("expired sealed");
    let rec = &expired.3;
    assert_eq!(rec["action_id"], format!("approval/{approval}/expired"));
    assert_eq!(rec["context"]["record_type"], "ai.brutor.approval-expired");
    assert_eq!(rec["context"]["approval_request_id"], approval.as_str());
    assert_eq!(rec["disposition"]["authority"], approval.as_str());
    assert_eq!(rec["chain"]["relation"], "supersedes");
    assert_eq!(rec["chain"]["parent_record_id"], awaiting_id.as_str());
    assert_verifies(rec);

    // Idempotent: a second sweep neither expires nor seals again.
    routes::approvals::expire_pending_approvals(&pool)
        .await
        .unwrap();
    let again = records(&pool, t).await;
    assert_eq!(again.iter().filter(|r| r.2 == "expired").count(), 1);
    let (executions,): (i64,) =
        sqlx::query_as("SELECT COUNT(*) FROM agent_skill_executions WHERE tenant_id = $1")
            .bind(t)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(executions, 0);
}

// ---------------------------------------------------------------------------
// Two-phase actions
// ---------------------------------------------------------------------------

pub(crate) async fn dispatch_and_log(
    pool: &PgPool,
    writer: &BackgroundWriter,
    tenant: &str,
    rg: &str,
) -> String {
    let pool2 = pool.clone();
    let tenant = tenant.to_string();
    let rg = rg.to_string();
    request::scope(None, async move {
        planned::dispatching(
            &pool2,
            &tenant,
            Some(&rg),
            "mcp",
            "write_order",
            Some("run-2p".into()),
        );
        // The outcome row, built in the same request scope, takes the id
        // reserved for it at dispatch.
        let mut log = proxy_logger::build_proxy_log(
            "mcp",
            &test_auth(&tenant),
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
        log.mcp_method = Some("tools/call".into());
        log.mcp_capability_name = Some("write_order".into());
        log.resource_group_id = Some(rg.clone());
        log.request_body_preview = Some(r#"{"order_id":"PO-9"}"#.into());
        log.response_body_preview = Some(r#"{"ok":true}"#.into());
        let id = log.id.clone();
        writer.write_proxy_log(pool2.clone(), log);
        id
    })
    .await
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn two_phase_system_seals_planned_then_a_confirming_outcome(pool: PgPool) {
    enable_evidence();
    let t = "tenant-ev-2p";
    common::seed_tenant(&pool, t).await;
    seed_ai_system(&pool, t, "sys-2p", json!({"two_phase_actions": true})).await;
    let writer = BackgroundWriter::new();
    let row_id = dispatch_and_log(&pool, &writer, t, "sys-2p").await;

    let (outcome_id,): (String,) = wait_for_row(
        &pool,
        "SELECT record_id FROM proxy_logs WHERE id = $1 AND record_id IS NOT NULL",
        &row_id,
    )
    .await;
    let recs = records(&pool, t).await;
    let planned = recs
        .iter()
        .find(|r| r.2 == "planned")
        .expect("planned record");
    let outcome = recs.iter().find(|r| r.0 == outcome_id).expect("outcome");
    assert_eq!(outcome.2, "executed");
    assert_eq!(planned.3["action_id"], format!("mcp/{row_id}"));
    assert_eq!(outcome.3["action_id"], planned.3["action_id"]);
    assert_eq!(planned.3["effect"]["status"], "planned");
    assert!(planned.3["effect"].get("request_digest").is_none());
    assert_eq!(outcome.3["chain"]["relation"], "confirms");
    assert_eq!(outcome.3["chain"]["parent_record_id"], planned.0.as_str());
    assert!(planned.1 < outcome.1, "planned is sealed first");
    assert_eq!(planned.3["system"]["ai_system_id"], "sys-2p");
    // Profile says nothing about salting and the system is not high-risk:
    // the outcome cites the plain content digests.
    assert!(outcome.3["attestation"].get("digest_salt").is_none());
    assert_verifies(&planned.3);
    assert_verifies(&outcome.3);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn single_phase_system_seals_only_the_outcome(pool: PgPool) {
    enable_evidence();
    let t = "tenant-ev-1p";
    common::seed_tenant(&pool, t).await;
    seed_ai_system(&pool, t, "sys-1p", json!({"two_phase_actions": false})).await;
    let writer = BackgroundWriter::new();
    let row_id = dispatch_and_log(&pool, &writer, t, "sys-1p").await;
    let _: (String,) = wait_for_row(
        &pool,
        "SELECT record_id FROM proxy_logs WHERE id = $1 AND record_id IS NOT NULL",
        &row_id,
    )
    .await;
    let recs = records(&pool, t).await;
    assert!(recs.iter().all(|r| r.2 != "planned"));
    let outcome = recs.iter().find(|r| r.2 == "executed").unwrap();
    assert!(outcome.3.get("chain").is_none());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn salted_system_seals_salted_digests_computed_before_retention(pool: PgPool) {
    enable_evidence();
    let t = "tenant-ev-salt";
    common::seed_tenant(&pool, t).await;
    seed_ai_system(&pool, t, "sys-salt", json!({"salt_digests": true})).await;
    let writer = BackgroundWriter::new();
    let row_id = dispatch_and_log(&pool, &writer, t, "sys-salt").await;
    let (rid, req_hash): (String, Option<String>) = wait_for_row(
        &pool,
        "SELECT record_id, request_content_hash FROM proxy_logs \
         WHERE id = $1 AND record_id IS NOT NULL",
        &row_id,
    )
    .await;
    let recs = records(&pool, t).await;
    let rec = &recs.iter().find(|r| r.0 == rid).unwrap().3;
    let salt = rec["attestation"]["digest_salt"].as_str().expect("salt");
    assert_eq!(salt.len(), 32);
    let input = rec["attestation"]["input_digest"].as_str().unwrap();
    assert_ne!(
        Some(input),
        req_hash.as_deref(),
        "not the unsalted row digest"
    );
    let (v, _) = brutor_evidence::canon::body_value(r#"{"order_id":"PO-9"}"#);
    assert_eq!(
        brutor_evidence::canon::salted_digest_hex(&v, salt).unwrap(),
        input,
        "recomputable from the captured value and the salt"
    );
    assert_verifies(rec);
}

// ---------------------------------------------------------------------------
// Privileged reads: a read-only tool seals only on a sensitive RG
// ---------------------------------------------------------------------------

/// A plain resource group, optionally flagged `sensitive_data`.
pub(crate) async fn seed_rg(pool: &PgPool, tenant: &str, id: &str, sensitive: bool) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, hierarchy_level, inherit_from_parent, \
         name, is_active, is_system, sensitive_data, phi_resource) \
         VALUES ($1, $2, 0, false, $1, true, false, $3, false)",
    )
    .bind(id)
    .bind(tenant)
    .bind(sensitive)
    .execute(pool)
    .await
    .expect("seed rg");
}

/// Write a `tools/call` row for a tool the server annotates
/// `readOnlyHint: true`, and return (row id, record id once settled).
pub(crate) async fn read_only_call(
    pool: &PgPool,
    tenant: &str,
    rg: &str,
) -> (String, Option<String>) {
    let server = format!("srv-ro-{}", Ulid::new()).to_lowercase();
    sqlx::query(
        "INSERT INTO mcp_servers (id, tenant_id, name, base_url, mcp_endpoint_path, enabled, \
         capabilities) VALUES ($1, $2, $1, 'http://test.local', '/mcp', true, $3::json)",
    )
    .bind(&server)
    .bind(tenant)
    .bind(
        json!({"tools": [{"name": "read_chart", "annotations": {"readOnlyHint": true}}]})
            .to_string(),
    )
    .execute(pool)
    .await
    .expect("seed mcp server");
    let mut log = proxy_logger::build_proxy_log(
        "mcp",
        &test_auth(tenant),
        "POST",
        &format!("/v1/proxy/mcp/{server}"),
        None,
        Some(&server),
        200,
        3.0,
        true,
        None,
        &format!("corr-{}", Ulid::new()),
    );
    log.mcp_method = Some("tools/call".into());
    log.mcp_capability_name = Some("read_chart".into());
    log.resource_group_id = Some(rg.to_string());
    let id = log.id.clone();
    BackgroundWriter::new().write_proxy_log(pool.clone(), log);
    // The writer mints in the same flush as the INSERT, so once the row is
    // visible its `record_id` is final.
    let (rid,): (Option<String>,) =
        wait_for_row(pool, "SELECT record_id FROM proxy_logs WHERE id = $1", &id).await;
    (id, rid)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn read_only_tool_is_sealed_only_on_a_sensitive_resource_group(pool: PgPool) {
    enable_evidence();
    let t = "tenant-ev-ro";
    common::seed_tenant(&pool, t).await;
    seed_rg(&pool, t, "rg-ro-sensitive", true).await;
    seed_rg(&pool, t, "rg-ro-plain", false).await;

    let (row, rid) = read_only_call(&pool, t, "rg-ro-sensitive").await;
    let rid = rid.expect("a read on a sensitive_data RG is a privileged read and is sealed");
    let recs = records(&pool, t).await;
    let rec = &recs.iter().find(|r| r.0 == rid).expect("record").3;
    assert_eq!(rec["action_id"], format!("mcp/{row}"));
    assert_eq!(rec["verdict"], "executed");
    assert_verifies(rec);

    let (_, rid) = read_only_call(&pool, t, "rg-ro-plain").await;
    assert!(
        rid.is_none(),
        "a read-only query on a normal RG is not sealed"
    );
    assert_eq!(records(&pool, t).await.len(), recs.len());
}

// ---------------------------------------------------------------------------
// Portal notice + AI-generated label + inference record
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_failed_completion_is_not_labelled_ai_generated(pool: PgPool) {
    let t = "tenant-ev-llm-4xx";
    common::seed_tenant(&pool, t).await;
    // No model bound: the proxy refuses before any generation happens.
    common::seed_resource_group(&pool, "rg-none", t).await;
    common::seed_end_user(&pool, 7805, t).await;
    common::add_direct_membership(&pool, 7805, "rg-none", t).await;
    let api_key = common::seed_api_key(&pool, t, "rg-none", Some(7805), "k").await;
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({"model_id": "m-missing", "messages": [{"role": "user", "content": "hi"}]}),
        Some(&api_key),
        Some(t),
    );
    let resp = common::build_llm_app(common::build_test_state(pool))
        .oneshot(req)
        .await
        .unwrap();
    assert!(!resp.status().is_success());
    assert!(resp.headers().get("x-brutor-ai-generated").is_none());
}

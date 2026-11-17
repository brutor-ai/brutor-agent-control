// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `skills__run_script` refuses a script that is not runnable in the skill
//! before any gate charges the call.
//!
//! The case: a traversal
//! test (`../../etc/passwd`) was refused, but only inside the runner client,
//! after the orchestrator had admitted the call against the group's skill
//! limits and recorded an execution. The execution was settled FAILED
//! `skill_runner_error` and the audit row said 502: a downstream failure, on
//! a request the gateway itself refused. A caller probing for scripts outside
//! the manifest therefore read as an unhealthy runner, consumed the group's
//! skill quota, and counted as a failed execution.
//!
//! The manifest allowlist now runs first: the row is a 403 refusal on the
//! caller's group and no execution exists.

use std::time::Duration;

use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use brutor_gateway_core::constants::system_server::agent_skill;

use crate::common;

const T: &str = "t-skill-allow";
const RG: &str = "rg-skill-allow";

async fn topology(pool: &PgPool) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 9400, T).await;
    common::add_direct_membership(pool, 9400, RG, T).await;
    common::seed_mcp_server_with_base_url(pool, "ms-skills", T, "skills", agent_skill::BASE_URL)
        .await;
    common::seed_server_config(pool, "sc-skills", T, "ms-skills").await;
    common::bind_server_config_to_rg(pool, "sc-skills", RG).await;
    common::seed_agent_skill(
        pool,
        "sk-report",
        T,
        "revenue-report",
        "# report",
        "Reports",
    )
    .await;
    common::bind_skill_to_rg(pool, "sk-report", RG).await;
    for (id, file, mode) in [
        ("scr-ok", "report.py", "SANDBOX"),
        ("scr-off", "retired.py", "DISABLED"),
    ] {
        sqlx::query(
            "INSERT INTO agent_skill_scripts \
             (id, tenant_id, skill_id, filename, script_content, script_hash, language, execution_mode) \
             VALUES ($1, $2, 'sk-report', $3, 'print(1)', 'h', 'PYTHON', $4::scriptexecutionmode)",
        )
        .bind(id)
        .bind(T)
        .bind(file)
        .bind(mode)
        .execute(pool)
        .await
        .expect("seed script");
    }
    common::seed_api_key(pool, T, RG, Some(9400), "k-skill-allow").await
}

/// Call `skills__run_script` for `script`; the JSON-RPC result.
async fn run_script(pool: &PgPool, key: &str, script: &str) -> Value {
    let app = common::build_mcp_proxy_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        "/v1/proxy/mcp/ms-skills",
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": agent_skill::TOOL_RUN_SCRIPT,
                          "arguments": {"skill_name": "revenue-report", "script": script,
                                        "args": {}}}}),
        Some(key),
        Some(T),
    );
    let resp = app.oneshot(req).await.expect("router call");
    assert_eq!(resp.status(), StatusCode::OK, "MCP transport answers 200");
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    serde_json::from_slice(&bytes).expect("json-rpc body")
}

/// The call's audit row: (status, group).
async fn audit_row(pool: &PgPool) -> (i32, Option<String>) {
    for _ in 0..100 {
        if let Some(row) = sqlx::query_as::<_, (i32, Option<String>)>(
            "SELECT http_status_code, resource_group_id FROM proxy_logs WHERE tenant_id = $1",
        )
        .bind(T)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs")
        {
            return row;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("no audit row");
}

async fn assert_refused_before_the_gates(pool: &PgPool, result: &Value, script: &str) {
    assert_eq!(
        result.pointer("/result/isError"),
        Some(&Value::Bool(true)),
        "{script}: refused: {result}"
    );
    let (status, group) = audit_row(pool).await;
    assert_eq!(
        status, 403,
        "{script}: a script the skill does not allow is a refusal, not a runner failure"
    );
    assert_eq!(
        group.as_deref(),
        Some(RG),
        "{script}: on the caller's group"
    );
    let (executions,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM agent_skill_executions WHERE tenant_id = $1")
            .bind(T)
            .fetch_one(pool)
            .await
            .expect("executions");
    assert_eq!(
        executions, 0,
        "{script}: nothing was admitted, so no execution exists"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_path_outside_the_skill_is_refused_before_the_gates(pool: PgPool) {
    let key = topology(&pool).await;
    let result = run_script(&pool, &key, "../../etc/passwd").await;
    assert_refused_before_the_gates(&pool, &result, "../../etc/passwd").await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_disabled_script_is_refused_before_the_gates(pool: PgPool) {
    let key = topology(&pool).await;
    let result = run_script(&pool, &key, "retired.py").await;
    assert_refused_before_the_gates(&pool, &result, "retired.py").await;
}

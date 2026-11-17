// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! An A2A v1 call that asserts no run is still a step of a run: the task it
//! lands on.
//!
//! The rows of such a call stamp the task as its own root, so that is the
//! run the ledger shows and an operator aborts. But the gates ran before the
//! task existed, and asked the run controls and the approval queue with no
//! run at all: an operator's abort of that task's run never stopped the next
//! message on it, a per-run cap never applied, and an approval hold named no
//! run (the ledger and the run clock could not see the wait). The gateway
//! now decides the run first — the named task, or a task id it mints for a
//! message that starts one — and the created task adopts that id.

use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use crate::common;

const T: &str = "t-v1run";
const CARD: &str = "card-v1run";
const CARD_NAME: &str = "v1run-agent";
const RG: &str = "rg-v1run";

/// Tenant, group, card (bound, one capability) and a key. With
/// `approval_grant` the key is an agent whose `a2a_call` on the card is held
/// for a human.
async fn topology(pool: &PgPool, approval_grant: bool) -> String {
    topology_in(pool, RG, approval_grant).await
}

/// [`topology`] in group `rg`. Run caps are cached process-wide by group
/// id, so a test that sets an envelope needs a group of its own.
async fn topology_in(pool: &PgPool, rg: &str, approval_grant: bool) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, rg, T).await;
    common::seed_end_user(pool, 9961, T).await;
    common::add_direct_membership(pool, 9961, rg, T).await;
    common::seed_agent_card(pool, CARD, T, CARD_NAME).await;
    common::bind_agent_card_to_rg(pool, CARD, rg).await;
    sqlx::query(
        "INSERT INTO agent_card_capabilities (id, agent_card_id, capability_name, handler_type, enabled) \
         VALUES ('cap-v1run', $1, 'echo', 'noop', true)",
    )
    .bind(CARD)
    .execute(pool)
    .await
    .expect("seed capability");
    let key = common::seed_api_key(pool, T, rg, Some(9961), "v1run-key").await;
    if approval_grant {
        sqlx::query(
            "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
             VALUES ('agent-v1run', $1, 'agent-v1run', 'manual', true, 'active', true)",
        )
        .bind(T)
        .execute(pool)
        .await
        .expect("agent identity");
        sqlx::query(
            "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
             VALUES ('g-v1run', $1, 'agent-v1run', 'a2a_call', $2, 'approval_required', true)",
        )
        .bind(T)
        .bind(CARD)
        .execute(pool)
        .await
        .expect("agent grant");
        sqlx::query("UPDATE api_keys SET agent_id = 'agent-v1run' WHERE tenant_id = $1")
            .bind(T)
            .execute(pool)
            .await
            .expect("bind key to agent");
    }
    key
}

/// `message:send` WITHOUT `x-brutor-run-id`; `task_id` names the task.
async fn send(pool: &PgPool, key: &str, task_id: Option<&str>) -> (StatusCode, Value) {
    let mut message = json!({"messageId": ulid::Ulid::new().to_string(), "role": "ROLE_USER",
                             "parts": [{"text": "hi"}]});
    if let Some(t) = task_id {
        message["taskId"] = json!(t);
    }
    let req = common::build_post_request(
        &format!("/a2a/{T}/{CARD_NAME}/message:send"),
        json!({ "message": message }),
        Some(key),
        Some(T),
    );
    let app = common::build_a2a_v1_app(common::build_test_state(pool.clone()));
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let bytes = resp
        .into_body()
        .collect()
        .await
        .expect("collect body")
        .to_bytes();
    let body = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, body)
}

/// The first value of `key` anywhere in `v`.
fn find<'a>(v: &'a Value, key: &str) -> Option<&'a Value> {
    match v {
        Value::Object(m) => m.get(key).or_else(|| m.values().find_map(|x| find(x, key))),
        Value::Array(a) => a.iter().find_map(|x| find(x, key)),
        _ => None,
    }
}

/// Poll for the newest row with `subtype` — the writer is batched.
async fn row_root(pool: &PgPool, subtype: &str) -> Option<Option<String>> {
    for _ in 0..80 {
        let row: Option<(Option<String>,)> = sqlx::query_as(
            "SELECT root_task_id FROM proxy_logs WHERE tenant_id = $1 AND proxy_subtype = $2 \
             ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(T)
        .bind(subtype)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs query");
        if let Some((root,)) = row {
            return Some(root);
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    None
}

async fn task_exists(pool: &PgPool, id: &str) -> bool {
    sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM a2a_tasks WHERE tenant_id = $1 AND id = $2")
        .bind(T)
        .bind(id)
        .fetch_one(pool)
        .await
        .expect("a2a_tasks query")
        > 0
}

async fn history_len(pool: &PgPool, id: &str) -> usize {
    let (task,): (Value,) =
        sqlx::query_as("SELECT task_json::jsonb FROM a2a_tasks WHERE tenant_id = $1 AND id = $2")
            .bind(T)
            .bind(id)
            .fetch_one(pool)
            .await
            .expect("task row");
    task["history"].as_array().map_or(0, Vec::len)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_new_task_is_the_run_its_gates_checked(pool: PgPool) {
    let key = topology(&pool, false).await;
    let (status, body) = send(&pool, &key, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let task_id = find(&body, "id")
        .and_then(Value::as_str)
        .expect("the response carries the task")
        .to_string();
    // The call's row names the task as its run — the id the gates used.
    let root = row_root(&pool, "v1-inbound").await.flatten();
    assert_eq!(root.as_deref(), Some(task_id.as_str()));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_operator_abort_of_a_task_run_stops_the_next_message_on_it(pool: PgPool) {
    let key = topology(&pool, false).await;
    let (status, body) = send(&pool, &key, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let task_id = find(&body, "id")
        .and_then(Value::as_str)
        .expect("task id")
        .to_string();
    let before = history_len(&pool, &task_id).await;

    // The operator stops the run the ledger shows: the task.
    sqlx::query(
        "INSERT INTO run_interventions \
         (id, tenant_id, root_task_id, action, reason, status, requested_at, \
          steps_blocked, created_at, updated_at) \
         VALUES ('ri-v1run', $1, $2, 'abort', 'runaway', 'requested', now(), 0, now(), now())",
    )
    .bind(T)
    .bind(&task_id)
    .execute(&pool)
    .await
    .expect("abort the run");

    // The caller continues the task without asserting a run.
    let (status, body) = send(&pool, &key, Some(&task_id)).await;
    assert!(
        status.is_client_error(),
        "a message on an aborted run must be refused, got {status}: {body}"
    );
    let root = row_root(&pool, "run_aborted").await;
    assert_eq!(
        root.flatten().as_deref(),
        Some(task_id.as_str()),
        "the refusal must be the run controls', on the task's run"
    );
    let (blocked,): (i32,) =
        sqlx::query_as("SELECT steps_blocked FROM run_interventions WHERE id = 'ri-v1run'")
            .fetch_one(&pool)
            .await
            .expect("intervention");
    assert!(blocked >= 1, "the intervention must record that it bit");
    assert_eq!(
        history_len(&pool, &task_id).await,
        before,
        "the refused message must not reach the task"
    );

    // Reading the stopped task is no step: the caller can still see it.
    let req = common::build_get_request(
        &format!("/a2a/{T}/{CARD_NAME}/tasks/{task_id}"),
        Some(&key),
        Some(T),
    );
    let app = common::build_a2a_v1_app(common::build_test_state(pool.clone()));
    let resp = app.oneshot(req).await.expect("router call");
    assert_eq!(
        resp.status(),
        StatusCode::OK,
        "an aborted run's task must stay readable"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_capped_run_is_checked_before_its_task_exists(pool: PgPool) {
    let rg = "rg-v1run-cap";
    let key = topology_in(&pool, rg, false).await;
    // The caller pre-allocates the task id; the run it starts is capped
    // (its one permitted step already spent).
    let task_id = ulid::Ulid::new().to_string();
    common::set_rg_operating_envelope(&pool, rg, json!({"max_llm_calls_per_run": 1})).await;
    let state = common::build_test_state(pool.clone());
    state.usage_state.run_commit(T, &task_id, 10, 0.0).await;

    let req = common::build_post_request(
        &format!("/a2a/{T}/{CARD_NAME}/message:send"),
        json!({"message": {"messageId": "01v1cap", "role": "ROLE_USER", "taskId": task_id,
                           "parts": [{"text": "hi"}]}}),
        Some(&key),
        Some(T),
    );
    let resp = common::build_a2a_v1_app(state.clone())
        .oneshot(req)
        .await
        .expect("router call");
    assert!(
        resp.status().is_client_error(),
        "a capped run must be refused, got {}",
        resp.status()
    );
    let root = row_root(&pool, "run_cap_reached").await;
    assert_eq!(root.flatten().as_deref(), Some(task_id.as_str()));
    assert!(
        !task_exists(&pool, &task_id).await,
        "a refused step must not create its task"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_hold_on_a_no_run_call_names_the_run_its_task_will_have(pool: PgPool) {
    let key = topology(&pool, true).await;
    let (status, body) = send(&pool, &key, None).await;
    assert!(
        !status.is_success(),
        "the call must be parked on a human, got {status}: {body}"
    );
    // The hold names a run, and the caller is told the task id to retry on.
    let (hold_root, hold_parent, hold_status): (Option<String>, Option<String>, String) =
        sqlx::query_as(
            "SELECT originating_trace_root_id, originating_parent_task_id, status \
             FROM tool_approval_requests WHERE tenant_id = $1",
        )
        .bind(T)
        .fetch_one(&pool)
        .await
        .expect("a hold was raised");
    assert_eq!(hold_status, "pending");
    let run = hold_root.expect("the hold must name its run");
    assert_eq!(
        hold_parent.as_deref(),
        Some(run.as_str()),
        "the step is the task the call creates"
    );
    assert_eq!(
        find(&body, "taskId").and_then(Value::as_str),
        Some(run.as_str()),
        "the caller must learn the task id to retry on: {body}"
    );
    // The hold's `awaiting_human` row lands on the same run.
    let root = row_root(&pool, "approval_required").await;
    assert_eq!(root.flatten().as_deref(), Some(run.as_str()));
    // The ledger sees the wait on that run.
    let held = brutor_gateway_core::services::run_ledger::held_ms_between(
        &pool,
        T,
        &run,
        crate::common::db_now() - chrono::Duration::hours(1),
        crate::common::db_now() + chrono::Duration::seconds(1),
    )
    .await
    .expect("held_ms_between");
    assert!(
        held > 0,
        "the run's hold time must be visible, got {held} ms"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_task_another_group_owns_is_not_a_run_the_caller_can_touch(pool: PgPool) {
    let key = topology(&pool, false).await;
    // A task in another group of the same tenant.
    common::seed_resource_group(&pool, "rg-v1run-other", T).await;
    sqlx::query(
        "INSERT INTO a2a_tasks (id, tenant_id, resource_group_id, context_id, state, \
                                status_timestamp, task_json) \
         VALUES ('task-other', $1, 'rg-v1run-other', 'ctx', 'TASK_STATE_WORKING', now(), \
                 '{\"id\":\"task-other\",\"status\":{\"state\":\"TASK_STATE_WORKING\"},\"history\":[]}'::json)",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("foreign task");
    sqlx::query(
        "INSERT INTO run_interventions \
         (id, tenant_id, root_task_id, action, reason, status, requested_at, \
          steps_blocked, created_at, updated_at) \
         VALUES ('ri-other', $1, 'task-other', 'abort', 'runaway', 'requested', now(), 0, now(), now())",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("abort the foreign run");

    let (status, _) = send(&pool, &key, Some("task-other")).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "no existence leak");
    let (blocked,): (i32,) =
        sqlx::query_as("SELECT steps_blocked FROM run_interventions WHERE id = 'ri-other'")
            .fetch_one(&pool)
            .await
            .expect("intervention");
    assert_eq!(
        blocked, 0,
        "a caller must not be able to act on another group's run"
    );
}

/// One request to a verb of the v1.0 route, with or without a run the
/// caller asserts.
async fn verb(
    pool: &PgPool,
    key: &str,
    method: &str,
    path: &str,
    body: Option<Value>,
    asserted_run: Option<&str>,
) -> StatusCode {
    let mut req = match body {
        Some(body) => common::build_post_request(path, body, Some(key), Some(T)),
        None => common::build_get_request(path, Some(key), Some(T)),
    };
    *req.method_mut() = method.parse().expect("method");
    if let Some(run) = asserted_run {
        req.headers_mut()
            .insert("x-brutor-run-id", run.parse().expect("header"));
    }
    let app = common::build_a2a_v1_app(common::build_test_state(pool.clone()));
    let resp = tokio::time::timeout(std::time::Duration::from_secs(20), app.oneshot(req))
        .await
        .expect("the verb answers")
        .expect("router call");
    resp.status()
}

/// A caller that asserts a run of its own (`x-brutor-run-id`) is still not
/// shown another group's task: the task's containment is decided for the
/// task, whatever run the caller names. Naming a run used to skip the check,
/// and the verbs that read the task without a group (subscribe, the push
/// configurations) then served it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn asserting_a_run_does_not_open_another_groups_task(pool: PgPool) {
    let key = topology(&pool, false).await;
    common::seed_resource_group(&pool, "rg-v1run-other", T).await;
    sqlx::query(
        "INSERT INTO a2a_tasks (id, tenant_id, resource_group_id, context_id, state, \
                                status_timestamp, task_json) \
         VALUES ('task-other', $1, 'rg-v1run-other', 'ctx', 'TASK_STATE_WORKING', now(), \
                 '{\"id\":\"task-other\",\"status\":{\"state\":\"TASK_STATE_WORKING\"},\"history\":[]}'::json)",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("foreign task");
    sqlx::query(
        "INSERT INTO a2a_push_notification_configs (id, tenant_id, task_id, config_json) \
         VALUES ('push-other', $1, 'task-other', \
                 '{\"id\":\"push-other\",\"taskId\":\"task-other\",\"url\":\"https://example.invalid/hook\"}'::json)",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("foreign push configuration");

    let base = format!("/a2a/{T}/{CARD_NAME}/tasks");
    let configs = "pushNotificationConfigs";
    let push = json!({"url": "https://example.invalid/mine"});
    let own_run = ulid::Ulid::new().to_string();
    for run in [None, Some(own_run.as_str())] {
        // What a task that does not exist answers each verb.
        for (method, tail, body) in [
            ("GET", String::new(), None),
            ("GET", ":subscribe".to_string(), None),
            ("POST", ":cancel".to_string(), Some(json!({}))),
            ("GET", format!("/{configs}"), None),
            ("POST", format!("/{configs}"), Some(push.clone())),
            ("GET", format!("/{configs}/push-other"), None),
            ("DELETE", format!("/{configs}/push-other"), None),
        ] {
            let missing = verb(
                &pool,
                &key,
                method,
                &format!("{base}/task-none{tail}"),
                body.clone(),
                run,
            )
            .await;
            let foreign = verb(
                &pool,
                &key,
                method,
                &format!("{base}/task-other{tail}"),
                body,
                run,
            )
            .await;
            assert_eq!(
                foreign, missing,
                "{method} tasks/task-other{tail} (asserted run: {run:?}): another group's \
                 task must be answered as a task that does not exist"
            );
            assert_eq!(
                foreign,
                StatusCode::NOT_FOUND,
                "{method} tasks/task-other{tail} (asserted run: {run:?}) was served"
            );
        }
    }
    let (state, configs_left): (String, i64) = sqlx::query_as(
        "SELECT (SELECT state FROM a2a_tasks WHERE id = 'task-other'), \
                (SELECT COUNT(*) FROM a2a_push_notification_configs WHERE task_id = 'task-other')",
    )
    .fetch_one(&pool)
    .await
    .expect("foreign task state");
    assert_eq!(
        state, "TASK_STATE_WORKING",
        "the foreign task was cancelled"
    );
    assert_eq!(
        configs_left, 1,
        "the foreign task's push configurations changed"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_streamed_output_block_is_recorded_on_the_task_run(pool: PgPool) {
    // A `message:stream` whose agent output the tenant's `a2a_outbound`
    // guardrail stops mid-stream. The stream's opening row reads as a
    // success; the block itself wrote no row at all.
    let rg = "rg-v1run-stream";
    let key = topology_in(&pool, rg, false).await;
    sqlx::query("UPDATE agent_card_capabilities SET handler_type = 'multi_turn_demo', handler_ref = 'demo' WHERE id = 'cap-v1run'")
        .execute(&pool)
        .await
        .expect("demo handler");
    common::seed_banned_words_guardrail_surfaces(
        &pool,
        "gr-v1run",
        T,
        &["your name"],
        &["a2a_outbound"],
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-v1run", rg).await;

    let req = common::build_post_request(
        &format!("/a2a/{T}/{CARD_NAME}/message:stream"),
        json!({"message": {"messageId": "01v1stream", "role": "ROLE_USER", "parts": [{"text": "hi"}]}}),
        Some(&key),
        Some(T),
    );
    let resp = common::build_a2a_v1_app(common::build_test_state(pool.clone()))
        .oneshot(req)
        .await
        .expect("router call");
    assert_eq!(resp.status(), StatusCode::OK);
    let body = tokio::time::timeout(
        std::time::Duration::from_secs(30),
        resp.into_body().collect(),
    )
    .await
    .expect("the blocked stream must end")
    .expect("collect")
    .to_bytes();
    let body = String::from_utf8_lossy(&body);
    assert!(
        !body.contains("your name"),
        "the blocked text must not leave: {body}"
    );

    let (task_id,): (String,) = sqlx::query_as("SELECT id FROM a2a_tasks WHERE tenant_id = $1")
        .bind(T)
        .fetch_one(&pool)
        .await
        .expect("the stream's task");
    let mut row = None;
    for _ in 0..80 {
        row = sqlx::query_as::<_, (Option<String>, Option<String>)>(
            "SELECT root_task_id, current_task_id FROM proxy_logs \
             WHERE tenant_id = $1 AND guardrail_blocked",
        )
        .bind(T)
        .fetch_optional(&pool)
        .await
        .expect("proxy_logs query");
        if row.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (root, current) = row.expect("the streamed block must write its row");
    assert_eq!(root.as_deref(), Some(task_id.as_str()), "on the task's run");
    assert_eq!(
        current.as_deref(),
        Some(task_id.as_str()),
        "naming its step"
    );
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Item 5 — A2A event-driven streaming publisher.
//!
//! `task_store::write` must fire `pg_notify('a2a_task_update', …)` after every
//! task persist so SSE streams (on any replica) wake event-driven instead of
//! polling. This pins that the NOTIFY actually fires, on the right channel,
//! with a payload that round-trips to [`TaskUpdateNotice`] carrying the correct
//! tenant + task id.
//!
//! We LISTEN via `PgListener::connect_with(&pool)` so the listener shares the
//! same per-test database as the writer (pg_notify is per-database). The hub
//! fan-out + GC logic is unit-tested in `services::a2a_v1::stream_hub`.

use std::time::Duration;

use sqlx::PgPool;
use sqlx::postgres::PgListener;

use brutor_gateway_core::services::a2a_v1::stream_hub::{
    A2A_TASK_UPDATE_CHANNEL, TaskUpdateNotice,
};
use brutor_gateway_core::services::a2a_v1::task_store;
use brutor_gateway_core::services::a2a_v1::{Task, TaskState, TaskStatus};

use crate::common;

fn working_task(id: &str) -> Task {
    Task {
        id: id.into(),
        context_id: Some(format!("ctx-{id}")),
        status: TaskStatus {
            state: TaskState::TaskStateWorking,
            message: None,
            timestamp: None,
        },
        artifacts: Vec::new(),
        history: Vec::new(),
        metadata: None,
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn write_fires_task_update_notify(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-a2a").await;

    let task = working_task("task-notify-1");
    task_store::create(&pool, "tenant-a2a", None, &task)
        .await
        .expect("create task");

    // Start listening on the same database BEFORE the write that should notify.
    let mut listener = PgListener::connect_with(&pool)
        .await
        .expect("listener connect");
    listener
        .listen(A2A_TASK_UPDATE_CHANNEL)
        .await
        .expect("listen channel");

    // A state transition goes through `write` → should emit the NOTIFY.
    task_store::update_status(
        &pool,
        "tenant-a2a",
        "task-notify-1",
        TaskState::TaskStateCompleted,
        None,
    )
    .await
    .expect("update_status");

    let notification = tokio::time::timeout(Duration::from_secs(5), listener.recv())
        .await
        .expect("NOTIFY did not arrive within 5s — publisher missing?")
        .expect("listener recv error");

    assert_eq!(notification.channel(), A2A_TASK_UPDATE_CHANNEL);

    let notice: TaskUpdateNotice =
        serde_json::from_str(notification.payload()).expect("payload decodes to TaskUpdateNotice");
    assert_eq!(notice.tenant_id, "tenant-a2a");
    assert_eq!(notice.task_id, "task-notify-1");
}

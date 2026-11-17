// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Brutor Skill Runner binary entry point.
//!
//! Loads config from env, builds the axum app, and listens. The body
//! of the logic lives in the library crate so integration tests can
//! mount the same app without going through a real network.

use std::sync::Arc;

use brutor_skill_runner::env_registry::{self, Env};
use brutor_skill_runner::{api, config::Config, env_vars, logging};
use tower_http::trace::TraceLayer;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // `--print-env`: the registry of environment variables as JSON, for the
    // packages' contract test and the documentation. Reads no variable.
    if env_registry::print_requested() {
        print!("{}", env_vars::REGISTRY.json_text());
        return Ok(());
    }

    // Load .env in dev only — Docker runs always get config from
    // proper env vars, never from a leftover dotfile.
    let _ = dotenvy::dotenv();

    // The environment is read once, here. A malformed value or a missing
    // required variable stops the runner, one line per name.
    let env = Env::load(&env_vars::REGISTRY)?;

    logging::init(&env);
    deny_same_uid_inspection()?;

    let config = Config::from_env(&env)?;
    let listen_addr = config.listen_addr;

    tracing::info!(
        addr = %listen_addr,
        max_request_bytes = brutor_skill_runner::config::MAX_REQUEST_BYTES,
        sandbox_timeout_secs = config.sandbox_timeout_secs,
        trusted_timeout_secs = config.trusted_timeout_secs,
        "starting brutor-skill-runner"
    );

    let state = api::AppState::new(config);
    let app = api::build_router(state).layer(TraceLayer::new_for_http());

    let listener = tokio::net::TcpListener::bind(listen_addr).await?;
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

/// Make this process non-dumpable before any skill runs.
///
/// Skill subprocesses run as the runner's own UID, so without this a skill
/// could read `/proc/1/environ` (the runner's environment, which holds
/// `BRUTOR_SKILL_RUNNER_JWT_SECRET` and, on Fargate, the path to the task's role
/// credentials) or ptrace the runner. A non-dumpable process's `/proc`
/// entries belong to root and it cannot be ptraced by its UID. The flag is
/// not inherited across `execve`, so skills are unaffected. Clearing the
/// child's environment does not cover this: the runner's own stays readable.
#[cfg(target_os = "linux")]
fn deny_same_uid_inspection() -> anyhow::Result<()> {
    // SAFETY: prctl(PR_SET_DUMPABLE, 0) takes no pointers and only changes
    // this process's dumpable flag.
    let rc = unsafe { libc::prctl(libc::PR_SET_DUMPABLE, 0, 0, 0, 0) };
    if rc != 0 {
        anyhow::bail!(
            "prctl(PR_SET_DUMPABLE, 0) failed: {}; refusing to run skills",
            std::io::Error::last_os_error()
        );
    }
    Ok(())
}

/// Development hosts (macOS) have no prctl; production images are Linux.
#[cfg(not(target_os = "linux"))]
fn deny_same_uid_inspection() -> anyhow::Result<()> {
    tracing::warn!("not Linux: the runner's environment is readable by the skills it runs");
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("ctrl_c handler install")
    };
    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => tracing::info!("shutting down (ctrl-c)"),
        _ = terminate => tracing::info!("shutting down (SIGTERM)"),
    }
    // Drop Arc to satisfy unused-binding lints in some toolchains.
    let _ = Arc::new(());
}

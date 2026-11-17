// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

#![forbid(unsafe_code)]
// No `unsafe`, enforced by the compiler.

//! `run-backfill` — reconstruct the run ledger from historical
//! `proxy_logs`.
//!
//! `proxy_logs` accumulates from the moment a gateway is installed,
//! regardless of when assurance is switched on. This turns that history
//! into runs, so a customer enabling the feature in month two gets
//! baselines immediately instead of waiting three weeks, and a demo shows
//! a populated board instead of an empty learning-mode panel.
//!
//! Lives here rather than in the control plane because it reuses the live
//! finalizer verbatim. A separate batch implementation would be a second
//! definition of what a run *is*, and the two would drift into quietly
//! disagreeing numbers.
//!
//! ```text
//! run-backfill --from 2026-06-01T00:00:00Z --to 2026-07-27T00:00:00Z
//! run-backfill --days 30 --batch-size 200
//! run-backfill --dry-run --days 7
//! ```
//!
//! Off by default and safe to re-run: a run already closed live is never
//! overwritten, so an interrupted pass can simply be started again.

use chrono::{DateTime, Duration, Utc};
use sqlx::postgres::PgPoolOptions;

use brutor_gateway_core::services::run_ledger;

struct Args {
    from: DateTime<Utc>,
    to: DateTime<Utc>,
    batch_size: i64,
    dry_run: bool,
}

fn usage() -> ! {
    // A raw string, not `\`-continued lines: the line-continuation escape
    // strips leading whitespace, which silently flattens the indentation
    // that makes usage text readable.
    eprintln!(
        r#"run-backfill — rebuild ai_system_runs from proxy_logs

USAGE:
  run-backfill [--from <RFC3339>] [--to <RFC3339>] [--days N]
               [--batch-size N] [--dry-run]

OPTIONS:
  --from        Start of the window (default: 30 days ago)
  --to          End of the window (default: now)
  --days N      Shorthand for --from <N days ago>
  --batch-size  Runs per batch (default: 500)
  --dry-run     Count what would be backfilled, write nothing

DATABASE_URL must be set."#
    );
    std::process::exit(2);
}

fn parse_args() -> Args {
    let mut from: Option<DateTime<Utc>> = None;
    let mut to: Option<DateTime<Utc>> = None;
    let mut days: Option<i64> = None;
    let mut batch_size: i64 = 500;
    let mut dry_run = false;

    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < argv.len() {
        match argv[i].as_str() {
            "--help" | "-h" => usage(),
            "--dry-run" => dry_run = true,
            "--from" | "--to" | "--days" | "--batch-size" => {
                let Some(v) = argv.get(i + 1) else {
                    eprintln!("error: {} needs a value", argv[i]);
                    usage();
                };
                match argv[i].as_str() {
                    "--from" | "--to" => {
                        let parsed = match DateTime::parse_from_rfc3339(v) {
                            Ok(d) => d.with_timezone(&Utc),
                            Err(e) => {
                                eprintln!("error: {} is not RFC3339 ({e})", v);
                                usage();
                            }
                        };
                        if argv[i] == "--from" {
                            from = Some(parsed);
                        } else {
                            to = Some(parsed);
                        }
                    }
                    "--days" => {
                        days = Some(match v.parse() {
                            Ok(n) => n,
                            Err(_) => {
                                eprintln!("error: --days must be an integer");
                                usage();
                            }
                        })
                    }
                    _ => {
                        batch_size = match v.parse() {
                            Ok(n) => n,
                            Err(_) => {
                                eprintln!("error: --batch-size must be an integer");
                                usage();
                            }
                        }
                    }
                }
                i += 1;
            }
            other => {
                eprintln!("error: unknown argument {other}");
                usage();
            }
        }
        i += 1;
    }

    let to = to.unwrap_or_else(Utc::now);
    let from = from.unwrap_or_else(|| to - Duration::days(days.unwrap_or(30)));

    if from >= to {
        eprintln!("error: --from must be before --to");
        std::process::exit(2);
    }
    if batch_size <= 0 {
        eprintln!("error: --batch-size must be positive");
        std::process::exit(2);
    }

    Args {
        from,
        to,
        batch_size,
        dry_run,
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // The same environment rules as the gateway: a retired or removed name
    // and a malformed value stop the tool before it connects. Of the
    // required variables it needs DATABASE_URL only.
    let env = brutor_gateway_core::env_vars::load_partial()?;

    tracing_subscriber::fmt()
        .with_env_filter(if env.is_set(&brutor_gateway_core::env_vars::RUST_LOG) {
            brutor_gateway_core::services::telemetry::log_filter(&env)
        } else {
            // The tool's own lines are not the gateway's targets.
            tracing_subscriber::EnvFilter::new("info")
        })
        .init();

    let args = parse_args();

    let database_url = env.required_string(&brutor_gateway_core::env_vars::DATABASE_URL)?;

    // A small pool: this is a background job and must not starve the
    // proxy's own connections if it happens to run against a live DB.
    let db = PgPoolOptions::new()
        .max_connections(4)
        .acquire_timeout(std::time::Duration::from_secs(10))
        .connect(&database_url)
        .await?;

    if args.dry_run {
        let pending: (i64,) = sqlx::query_as(
            "SELECT COUNT(*) FROM (
                 SELECT pl.tenant_id, pl.root_task_id
                   FROM proxy_logs pl
                   LEFT JOIN ai_system_runs r
                          ON r.tenant_id = pl.tenant_id
                         AND r.root_task_id = pl.root_task_id
                  WHERE pl.root_task_id IS NOT NULL
                    AND pl.timestamp >= $1 AND pl.timestamp < $2
                  GROUP BY pl.tenant_id, pl.root_task_id
                 HAVING MAX(r.id) IS NULL
             ) s",
        )
        .bind(args.from)
        .bind(args.to)
        .fetch_one(&db)
        .await?;

        println!(
            "dry-run: {} run(s) would be backfilled between {} and {}",
            pending.0,
            args.from.to_rfc3339(),
            args.to.to_rfc3339()
        );
        return Ok(());
    }

    println!(
        "backfilling runs from {} to {} (batch {})",
        args.from.to_rfc3339(),
        args.to.to_rfc3339(),
        args.batch_size
    );

    let started = std::time::Instant::now();
    let progress = run_ledger::backfill_runs(&db, args.from, args.to, args.batch_size, |p| {
        // Progress is printed per batch rather than per run: a long
        // backfill is a job an operator watches, and a line per run would
        // bury the summary.
        println!(
            "  … scanned={} finalized={} skipped={} failed={}",
            p.scanned, p.finalized, p.skipped, p.failed
        );
    })
    .await;

    println!(
        "done in {:.1}s — scanned={} finalized={} skipped={} failed={}",
        started.elapsed().as_secs_f64(),
        progress.scanned,
        progress.finalized,
        progress.skipped,
        progress.failed
    );

    if progress.failed > 0 {
        // A non-zero exit so a wrapper script or cron notices, without
        // pretending the whole pass failed — the successful runs are
        // already committed.
        std::process::exit(1);
    }
    Ok(())
}
